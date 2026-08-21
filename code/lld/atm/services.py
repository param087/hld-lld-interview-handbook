"""The peripherals and the ATM: the State context that ties every other module together."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from common import Clock, ConflictError, Money, SystemClock
from lld.atm.bank import BankService
from lld.atm.dispenser import CashDispenser
from lld.atm.models import (
    AtmStateError,
    AtmStateName,
    Card,
    CardStatus,
    Receipt,
    SessionTimeoutError,
    TransactionRecord,
    UnknownAccountError,
)
from lld.atm.states import ATMState, state_for


# --8<-- [start:peripherals]
class Screen:
    """The display. Keeping it an object means the flow never calls ``print``."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    def show(self, message: str) -> None:
        self._lines.append(message)

    def last(self) -> str:
        return self._lines[-1] if self._lines else ""

    def lines(self) -> list[str]:
        return list(self._lines)


class Printer:
    def __init__(self) -> None:
        self._receipts: list[Receipt] = []

    def print_receipt(self, receipt: Receipt) -> str:
        self._receipts.append(receipt)
        return receipt.render()

    def receipts(self) -> list[Receipt]:
        return list(self._receipts)


class CardReader:
    def __init__(self) -> None:
        self.card: Card | None = None
        self.retained: list[Card] = []

    def accept(self, card: Card) -> None:
        if self.card is not None:
            raise ConflictError("the reader already holds a card")
        self.card = card

    def eject(self) -> Card | None:
        card, self.card = self.card, None
        return card

    def retain(self, card: Card) -> None:
        """Swallow the card. Where it physically is beats why it got here."""
        card.status = CardStatus.RETAINED
        self.card = None
        self.retained.append(card)


# --8<-- [end:peripherals]


# --8<-- [start:atm]
class ATM:
    """The State context. One RLock serialises the whole machine.

    A physical ATM has one customer at a time, but the software has several callers:
    the customer's keypad, the maintenance panel and a session-timeout watchdog. The
    lock is what stops the watchdog from ejecting the card mid-dispense.
    """

    SESSION_TIMEOUT_SECONDS = 90.0

    def __init__(
        self,
        atm_id: str,
        bank: BankService,
        dispenser: CashDispenser,
        screen: Screen | None = None,
        printer: Printer | None = None,
        reader: CardReader | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.id = atm_id
        self.bank = bank
        self.dispenser = dispenser
        self.screen = screen or Screen()
        self.printer = printer or Printer()
        self.reader = reader or CardReader()
        self.fault: str | None = None
        self._clock = clock or SystemClock()
        self._state: ATMState = state_for(AtmStateName.IDLE)
        self._accounts: tuple[str, ...] = ()
        self._selected: str | None = None
        self._last_activity = self._clock.now()
        self._lock = threading.RLock()

    # -- the public keypad API ---------------------------------------------------------
    def insert_card(self, card: Card) -> None:
        with self._session() as state:
            state.insert_card(self, card)

    def enter_pin(self, pin: str) -> None:
        with self._session() as state:
            state.enter_pin(self, pin)

    def select_account(self, account_id: str) -> None:
        with self._session() as state:
            state.select_account(self, account_id)

    def check_balance(self) -> Money:
        with self._session() as state:
            return state.check_balance(self)

    def withdraw(self, amount: Money) -> Receipt:
        with self._session() as state:
            return state.withdraw(self, amount)

    def deposit(self, amount: Money) -> Receipt:
        with self._session() as state:
            return state.deposit(self, amount)

    def transfer(self, target_id: str, amount: Money) -> Receipt:
        with self._session() as state:
            return state.transfer(self, target_id, amount)

    def mini_statement(self) -> list[TransactionRecord]:
        with self._session() as state:
            return state.mini_statement(self)

    def cancel(self) -> None:
        with self._session() as state:
            state.cancel(self)

    # -- state plumbing ----------------------------------------------------------------
    @property
    def state(self) -> AtmStateName:
        with self._lock:
            return self._state.name

    def enter(self, name: AtmStateName) -> None:
        with self._lock:
            self._state = state_for(name)

    def begin_session(self, account_ids: tuple[str, ...]) -> None:
        self._accounts = account_ids
        self._selected = account_ids[0] if account_ids else None

    def set_account(self, account_id: str) -> None:
        """Called by ``AuthenticatedState``; the keypad entry point is ``select_account``."""
        if account_id not in self._accounts:
            raise UnknownAccountError(f"card does not reach account {account_id}")
        self._selected = account_id

    def end_session(self) -> Card | None:
        card = self.reader.eject()
        self._accounts, self._selected = (), None
        self.screen.show("card returned")
        self.enter(AtmStateName.IDLE)
        return card

    def go_out_of_service(self, fault: str) -> None:
        self.fault = fault
        self.reader.eject()
        self._accounts, self._selected = (), None
        self.screen.show(f"out of service: {fault}")
        self.enter(AtmStateName.OUT_OF_SERVICE)

    def replenish(self, notes: Mapping[Money, int]) -> Money:
        """Admin action: refill the cassettes and bring the machine back."""
        with self._lock:
            total = self.dispenser.replenish(notes)
            self.fault = None
            self.enter(AtmStateName.IDLE)
            return total

    def require_card(self) -> Card:
        card = self.reader.card
        if card is None:
            raise AtmStateError("no card in the reader")
        return card

    def require_account(self) -> str:
        if self._selected is None:
            raise AtmStateError("no account selected")
        return self._selected

    @contextmanager
    def _session(self) -> Iterator[ATMState]:
        """Lock, expire an abandoned session, run the operation, then stamp the activity."""
        with self._lock:
            self._expire_if_abandoned()
            try:
                yield self._state
            finally:
                self._last_activity = self._clock.now()

    def _expire_if_abandoned(self) -> None:
        idle_for = self._clock.now() - self._last_activity
        if self._state.name in (AtmStateName.IDLE, AtmStateName.OUT_OF_SERVICE):
            return
        if idle_for > self.SESSION_TIMEOUT_SECONDS:
            self.end_session()
            raise SessionTimeoutError(f"session dropped after {idle_for:.0f}s; card returned")


# --8<-- [end:atm]
