"""The bank behind the machine: a Facade Protocol and an in-memory implementation.

The ATM never talks to accounts directly. Everything it needs is eight methods, three
of which - ``reserve``, ``commit`` and ``release`` - are the atomicity story of this
whole design.
"""

from __future__ import annotations

import threading
from typing import Protocol

from common import (
    Clock,
    ConflictError,
    IdGenerator,
    Money,
    SequentialIdGenerator,
    SystemClock,
    ValidationError,
)
from lld.atm.models import (
    SECONDS_PER_DAY,
    Account,
    Card,
    CardBlockedError,
    CardStatus,
    DailyLimitExceededError,
    InsufficientFundsError,
    InvalidPinError,
    Reservation,
    TransactionRecord,
    TransactionStatus,
    TransactionType,
    UnknownAccountError,
)


# --8<-- [start:bank]
class BankService(Protocol):
    """What the machine needs from the core banking system - nothing more.

    A Facade: the real thing is a dozen hosts and a message queue. The ATM sees
    eight methods, which is also what makes it testable.
    """

    def authenticate(self, card_number: str, pin: str) -> tuple[str, ...]: ...

    def balance(self, account_id: str) -> Money: ...

    def reserve(self, account_id: str, amount: Money) -> Reservation: ...

    def commit(self, reservation: Reservation) -> TransactionRecord: ...

    def release(self, reservation: Reservation) -> None: ...

    def deposit(self, account_id: str, amount: Money) -> TransactionRecord: ...

    def transfer(self, source_id: str, target_id: str, amount: Money) -> TransactionRecord: ...

    def statement(self, account_id: str, limit: int) -> list[TransactionRecord]: ...


class InMemoryBank:
    """One lock per account, plus one registry lock for the shared dictionaries.

    Transfers take two account locks, always in id order, so two opposite transfers
    can never deadlock.
    """

    MAX_PIN_ATTEMPTS = 3

    def __init__(self, clock: Clock | None = None, ids: IdGenerator | None = None) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("TXN")
        self._accounts: dict[str, Account] = {}
        self._account_locks: dict[str, threading.Lock] = {}
        self._cards: dict[str, Card] = {}
        self._pins: dict[str, str] = {}
        self._attempts: dict[str, int] = {}
        self._reservations: dict[str, Reservation] = {}
        self._ledger: dict[str, list[TransactionRecord]] = {}
        self._registry_lock = threading.Lock()

    # -- setup -------------------------------------------------------------------------
    def open_account(self, account_id: str, holder: str, balance: Money, **limits: Money) -> Account:
        account = Account(id=account_id, holder=holder, balance=balance, **limits)
        with self._registry_lock:
            self._accounts[account_id] = account
            self._account_locks[account_id] = threading.Lock()
            self._ledger[account_id] = []
        return account

    def issue_card(self, number: str, holder: str, pin: str, account_ids: tuple[str, ...]) -> Card:
        card = Card(number=number, holder=holder, account_ids=account_ids)
        with self._registry_lock:
            self._cards[number] = card
            self._pins[number] = pin
        return card

    # -- authentication ----------------------------------------------------------------
    def authenticate(self, card_number: str, pin: str) -> tuple[str, ...]:
        with self._registry_lock:
            card = self._cards.get(card_number)
            if card is None:
                raise UnknownAccountError(f"unknown card {card_number}")
            if not card.is_usable():
                raise CardBlockedError(f"card {card_number} is {card.status}")
            if self._pins[card_number] != pin:
                self._attempts[card_number] = self._attempts.get(card_number, 0) + 1
                left = self.MAX_PIN_ATTEMPTS - self._attempts[card_number]
                if left <= 0:
                    card.status = CardStatus.BLOCKED
                    raise CardBlockedError(f"card {card_number} blocked after {self.MAX_PIN_ATTEMPTS} attempts")
                raise InvalidPinError(f"wrong PIN, {left} attempt(s) left")
            self._attempts.pop(card_number, None)
            return card.account_ids

    # -- reserve, commit, release ------------------------------------------------------
    def reserve(self, account_id: str, amount: Money) -> Reservation:
        """Hold the money before a single note moves. Visible to every other machine."""
        account = self._account(account_id)
        with self._lock_for(account_id):
            self._roll_day(account)
            if amount > account.available():
                raise InsufficientFundsError(f"{account_id} has {account.available()} available, needs {amount}")
            if amount > account.remaining_daily():
                raise DailyLimitExceededError(
                    f"{account_id} may still take {account.remaining_daily()} today, not {amount}"
                )
            account.reserved = account.reserved + amount
            reservation = Reservation(self._ids.next_id(), account_id, amount, self._clock.now())
        with self._registry_lock:
            self._reservations[reservation.id] = reservation
        return reservation

    def commit(self, reservation: Reservation) -> TransactionRecord:
        self._take_reservation(reservation)
        account = self._account(reservation.account_id)
        with self._lock_for(account.id):
            account.reserved = account.reserved - reservation.amount
            account.balance = account.balance - reservation.amount
            account.daily_withdrawn = account.daily_withdrawn + reservation.amount
            return self._record(TransactionType.WITHDRAWAL, account, reservation.amount)

    def release(self, reservation: Reservation) -> None:
        """Undo step 1. Idempotent: a retry after a jam must not credit twice."""
        self._take_reservation(reservation)
        account = self._account(reservation.account_id)
        with self._lock_for(account.id):
            account.reserved = account.reserved - reservation.amount
            self._record(
                TransactionType.WITHDRAWAL, account, reservation.amount, status=TransactionStatus.ROLLED_BACK
            )

    # -- other transactions ------------------------------------------------------------
    def balance(self, account_id: str) -> Money:
        with self._lock_for(account_id):
            return self._account(account_id).balance

    def reserved(self, account_id: str) -> Money:
        """Money promised to a withdrawal in flight. Should be zero between customers."""
        with self._lock_for(account_id):
            return self._account(account_id).reserved

    def deposit(self, account_id: str, amount: Money) -> TransactionRecord:
        account = self._account(account_id)
        with self._lock_for(account_id):
            account.balance = account.balance + amount
            return self._record(TransactionType.DEPOSIT, account, amount)

    def transfer(self, source_id: str, target_id: str, amount: Money) -> TransactionRecord:
        if source_id == target_id:
            raise ValidationError("cannot transfer to the same account")
        source, target = self._account(source_id), self._account(target_id)
        first, second = sorted((source_id, target_id))  # fixed lock order, no deadlock
        with self._lock_for(first), self._lock_for(second):
            if amount > source.available():
                raise InsufficientFundsError(f"{source_id} has {source.available()}, needs {amount}")
            source.balance = source.balance - amount
            target.balance = target.balance + amount
            self._record(TransactionType.TRANSFER, target, amount, counterparty=source_id)
            return self._record(TransactionType.TRANSFER, source, amount, counterparty=target_id)

    def record_inquiry(self, account_id: str) -> TransactionRecord:
        account = self._account(account_id)
        with self._lock_for(account_id):
            return self._record(TransactionType.BALANCE_INQUIRY, account, Money(0))

    def statement(self, account_id: str, limit: int = 5) -> list[TransactionRecord]:
        with self._registry_lock:
            return list(self._ledger.get(account_id, []))[-limit:]

    # -- internals ---------------------------------------------------------------------
    def _account(self, account_id: str) -> Account:
        with self._registry_lock:
            account = self._accounts.get(account_id)
        if account is None:
            raise UnknownAccountError(f"unknown account {account_id}")
        return account

    def _lock_for(self, account_id: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._account_locks.get(account_id)
        if lock is None:
            raise UnknownAccountError(f"unknown account {account_id}")
        return lock

    def _take_reservation(self, reservation: Reservation) -> None:
        with self._registry_lock:
            if self._reservations.pop(reservation.id, None) is None:
                raise ConflictError(f"reservation {reservation.id} was already settled")

    def _roll_day(self, account: Account) -> None:
        today = int(self._clock.now() // SECONDS_PER_DAY)
        if account.day_stamp != today:
            account.day_stamp = today
            account.daily_withdrawn = Money(0)

    def _record(
        self,
        transaction_type: TransactionType,
        account: Account,
        amount: Money,
        status: TransactionStatus = TransactionStatus.COMMITTED,
        counterparty: str | None = None,
    ) -> TransactionRecord:
        record = TransactionRecord(
            id=self._ids.next_id(),
            type=transaction_type,
            account_id=account.id,
            amount=amount,
            balance_after=account.balance,
            at=self._clock.now(),
            status=status,
            counterparty=counterparty,
        )
        with self._registry_lock:
            self._ledger.setdefault(account.id, []).append(record)
        return record


# --8<-- [end:bank]
