"""Enums, errors and entities for the wallet and the gateway.

Money is ``common.Money`` (integer cents) everywhere. Every balance change goes
through ``Wallet``, and every status change goes through ``Transaction`` -- both
guard their invariants so no service can push a wallet negative or skip a state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class TransactionType(StrEnum):
    TOP_UP = "top_up"  # money in, from a card, UPI or bank
    TRANSFER = "transfer"  # wallet to wallet, no external rail
    WITHDRAWAL = "withdrawal"  # money out, to a bank account
    MERCHANT_PAYMENT = "merchant_payment"  # wallet to merchant, minus a fee
    REFUND = "refund"  # merchant back to wallet


class TransactionStatus(StrEnum):
    INITIATED = "initiated"  # created, funds reserved, nothing settled
    AUTHORIZED = "authorized"  # the processor holds the money, we do not yet
    CAPTURED = "captured"  # settled, ledger posted, balances moved
    FAILED = "failed"  # declined or refused; every reservation released
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


class PaymentMethodType(StrEnum):
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"


class EntryDirection(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


TERMINAL_STATUSES = frozenset({TransactionStatus.FAILED, TransactionStatus.REFUNDED})

TRANSACTION_TRANSITIONS: Mapping[TransactionStatus, frozenset[TransactionStatus]] = {
    TransactionStatus.INITIATED: frozenset(
        {TransactionStatus.AUTHORIZED, TransactionStatus.CAPTURED, TransactionStatus.FAILED}
    ),
    TransactionStatus.AUTHORIZED: frozenset({TransactionStatus.CAPTURED, TransactionStatus.FAILED}),
    TransactionStatus.CAPTURED: frozenset(
        {TransactionStatus.PARTIALLY_REFUNDED, TransactionStatus.REFUNDED}
    ),
    TransactionStatus.PARTIALLY_REFUNDED: frozenset(
        {TransactionStatus.PARTIALLY_REFUNDED, TransactionStatus.REFUNDED}
    ),
    TransactionStatus.FAILED: frozenset(),
    TransactionStatus.REFUNDED: frozenset(),
}

# How far along a transaction a processor event claims to be. A webhook whose
# rank is not higher than the transaction's current rank is late or duplicate.
STATUS_RANK: Mapping[TransactionStatus, int] = {
    TransactionStatus.INITIATED: 0,
    TransactionStatus.AUTHORIZED: 1,
    TransactionStatus.FAILED: 2,
    TransactionStatus.CAPTURED: 2,
    TransactionStatus.PARTIALLY_REFUNDED: 3,
    TransactionStatus.REFUNDED: 3,
}


# --8<-- [end:enums]


# --8<-- [start:errors]
class InsufficientBalanceError(ConflictError):
    """Available balance (balance minus reservations) cannot cover the amount."""


class LedgerImbalanceError(ConflictError):
    """A posting whose debits and credits do not cancel. Never let this reach production."""


class IdempotencyConflictError(ConflictError):
    """The key was reused with a different request, or the first request is still running."""


class FraudRejectedError(ConflictError):
    """A fraud rule refused the payment before any money moved."""


class PaymentDeclinedError(ConflictError):
    """The processor refused the authorization; reservations are released."""


class TransactionStateError(InvalidStateError):
    """The transaction is not in a state that allows this transition."""


class UnknownEntityError(NotFoundError):
    """No such wallet, transaction or payment method."""


# --8<-- [end:errors]


# --8<-- [start:wallet]
@dataclass(slots=True)
class Wallet:
    """Balance plus reservations. ``debit`` is the only negative-balance guard you need."""

    id: str
    owner_id: str
    balance: Money
    reserved: Money = Money(0)

    def available(self) -> Money:
        return self.balance - self.reserved

    def reserve(self, amount: Money) -> None:
        _require_positive(amount)
        if amount > self.available():
            raise InsufficientBalanceError(f"{self.id}: {self.available()} available, {amount} requested")
        self.reserved = self.reserved + amount

    def release(self, amount: Money) -> None:
        self.reserved = Money(max(0, (self.reserved - amount).cents), amount.currency)

    def debit(self, amount: Money) -> None:
        _require_positive(amount)
        if amount > self.balance:
            raise InsufficientBalanceError(f"{self.id}: balance {self.balance} cannot cover {amount}")
        self.balance = self.balance - amount

    def credit(self, amount: Money) -> None:
        _require_positive(amount)
        self.balance = self.balance + amount

    def copy(self) -> Wallet:
        return Wallet(self.id, self.owner_id, self.balance, self.reserved)


def _require_positive(amount: Money) -> None:
    if amount.cents <= 0:
        raise ValidationError(f"amount must be positive, got {amount}")


# --8<-- [end:wallet]


# --8<-- [start:transaction]
@dataclass(frozen=True, slots=True)
class PaymentMethod:
    """A tokenised instrument. The token is what the processor understands, not a card number."""

    id: str
    owner_id: str
    type: PaymentMethodType
    token: str
    label: str


@dataclass(slots=True)
class Transaction:
    """One money-moving intent, from creation to settlement or refund."""

    id: str
    type: TransactionType
    status: TransactionStatus
    amount: Money
    source: str  # ledger account money leaves
    destination: str  # ledger account money arrives in
    idempotency_key: str
    created_at: float
    psp_reference: str | None = None
    failure_reason: str | None = None
    refunded: Money = Money(0)

    def transition_to(self, status: TransactionStatus) -> None:
        if status not in TRANSACTION_TRANSITIONS[self.status]:
            raise TransactionStateError(f"transaction {self.id}: {self.status} cannot become {status}")
        self.status = status

    def is_final(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def refundable(self) -> Money:
        if self.status not in (
            TransactionStatus.CAPTURED,
            TransactionStatus.PARTIALLY_REFUNDED,
        ):
            return Money(0, self.amount.currency)
        return self.amount - self.refunded

    def copy(self) -> Transaction:
        return Transaction(
            self.id, self.type, self.status, self.amount, self.source, self.destination,
            self.idempotency_key, self.created_at, self.psp_reference, self.failure_reason, self.refunded,
        )


@dataclass(frozen=True, slots=True)
class Refund:
    id: str
    transaction_id: str
    amount: Money
    idempotency_key: str
    at: float


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """What the processor posts back to us. ``event_id`` is its delivery key."""

    event_id: str
    psp_reference: str
    status: TransactionStatus
    at: float


# --8<-- [end:transaction]
