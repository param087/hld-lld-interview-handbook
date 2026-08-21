"""Enums, entities, value objects and domain errors for the ATM.

The money in this package is always ``common.Money`` (integer cents). Notes are
``Money`` values too, so "how many 50s" is arithmetic on the same type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from common import (
    ConflictError,
    HandbookError,
    InvalidStateError,
    Money,
    NotFoundError,
    ValidationError,
)

SECONDS_PER_DAY = 86_400


# --8<-- [start:enums]
class AtmStateName(StrEnum):
    """The six states a machine can be in. The classes live in ``states.py``."""

    IDLE = "idle"  # waiting for a card
    CARD_INSERTED = "card_inserted"  # card held, PIN not yet accepted
    AUTHENTICATED = "authenticated"  # a session is open on one account
    TRANSACTING = "transacting"  # a command is running against the bank
    DISPENSING = "dispensing"  # notes are moving; cancel is refused here
    OUT_OF_SERVICE = "out_of_service"  # jam or empty cassettes; only an admin gets out


class CardStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"  # three wrong PINs
    RETAINED = "retained"  # swallowed by the machine


class TransactionType(StrEnum):
    WITHDRAWAL = "withdrawal"
    DEPOSIT = "deposit"
    TRANSFER = "transfer"
    BALANCE_INQUIRY = "balance_inquiry"


class TransactionStatus(StrEnum):
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


# --8<-- [end:enums]


# --8<-- [start:errors]
class AtmStateError(InvalidStateError):
    """The operation is not offered in the machine's current state."""


class SessionTimeoutError(InvalidStateError):
    """The customer walked away; the card was ejected and the session dropped."""


class InvalidPinError(ValidationError):
    """Wrong PIN; the message says how many attempts are left."""


class CardBlockedError(ConflictError):
    """Three wrong PINs, or a card the bank has already blocked."""


class InsufficientFundsError(ConflictError):
    """The available balance (balance minus reservations) is too small."""


class DailyLimitExceededError(ConflictError):
    """This withdrawal would push the account past its 24-hour cash limit."""


class DenominationError(ConflictError):
    """The amount cannot be built from the notes this machine holds."""


class OutOfCashError(ConflictError):
    """The cassettes hold less than the requested amount."""


class DispenserJamError(HandbookError):
    """The hardware failed while picking notes; nothing left the machine."""


class UnknownAccountError(NotFoundError):
    """No such account or card."""


# --8<-- [end:errors]


# --8<-- [start:entities]
@dataclass(slots=True)
class Card:
    number: str
    holder: str
    account_ids: tuple[str, ...]
    status: CardStatus = CardStatus.ACTIVE

    def is_usable(self) -> bool:
        return self.status is CardStatus.ACTIVE


@dataclass(slots=True)
class Account:
    """Balance and reservations are separate on purpose.

    ``reserved`` is money promised to a withdrawal that has not been dispensed yet.
    A second machine sees it immediately through ``available()``, which is how the
    same account cannot be emptied twice at the same moment.
    """

    id: str
    holder: str
    balance: Money
    reserved: Money = Money(0)
    daily_withdrawn: Money = Money(0)
    daily_limit: Money = Money.of("500.00")
    day_stamp: int = 0

    def available(self) -> Money:
        return self.balance - self.reserved

    def remaining_daily(self) -> Money:
        """Reservations count against the limit exactly as they count against the balance.

        Subtracting only ``daily_withdrawn`` would let two machines each reserve an
        amount under the limit and then both commit, taking the account over it.
        """
        return self.daily_limit - self.daily_withdrawn - self.reserved


@dataclass(frozen=True, slots=True)
class Reservation:
    """The receipt for step 1 of reserve-dispense-commit."""

    id: str
    account_id: str
    amount: Money
    created_at: float


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    id: str
    type: TransactionType
    account_id: str
    amount: Money
    balance_after: Money
    at: float
    status: TransactionStatus = TransactionStatus.COMMITTED
    counterparty: str | None = None

    def __str__(self) -> str:
        target = f" -> {self.counterparty}" if self.counterparty else ""
        return f"{self.id} {self.type}{target} {self.amount} balance {self.balance_after}"


@dataclass(frozen=True, slots=True)
class Receipt:
    atm_id: str
    record: TransactionRecord
    notes: dict[Money, int] = field(default_factory=dict)

    def note_summary(self) -> str:
        if not self.notes:
            return "no cash"
        return ", ".join(f"{count}x{note}" for note, count in sorted(self.notes.items(), reverse=True))

    def render(self) -> str:
        return f"[{self.atm_id}] {self.record} ({self.note_summary()})"


# --8<-- [end:entities]
