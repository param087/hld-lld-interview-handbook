"""Entities, value objects, enums and domain errors for the Splitwise clone.

Anything that touches more than one entity lives in ``services.py``; the split
rules live in ``strategies.py``. Money is always ``common.Money`` (integer
cents), so a three-way split of 100.01 never loses a cent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from common import InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class SplitType(StrEnum):
    EQUAL = "equal"  # no weights
    EXACT = "exact"  # weights are cents and must add up to the total
    PERCENT = "percent"  # weights are basis points and must add up to 10_000
    SHARE = "share"  # weights are arbitrary share units (2:1:1)


class ExpenseStatus(StrEnum):
    ACTIVE = "active"  # counted in the balances
    SUPERSEDED = "superseded"  # replaced by an edited version, kept for the audit trail
    DELETED = "deleted"  # reversed out of the balances


class ActivityKind(StrEnum):
    EXPENSE_ADDED = "expense_added"
    EXPENSE_EDITED = "expense_edited"
    EXPENSE_DELETED = "expense_deleted"
    EXPENSE_RESTORED = "expense_restored"
    SETTLED = "settled"


# --8<-- [end:enums]


# --8<-- [start:errors]
class UnbalancedExpenseError(ValidationError):
    """What was paid and what is owed do not add up to the same total."""


class GroupMembershipError(ValidationError):
    """A payer or participant does not belong to the group."""


class ExpenseStateError(InvalidStateError):
    """The expense is not in a state that allows the operation (editing a deleted one)."""


class UnknownEntityError(NotFoundError):
    """No such user, group or expense."""


# --8<-- [end:errors]


# --8<-- [start:entities]
@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    email: str


@dataclass(slots=True)
class Group:
    """A set of members that share expenses. One currency per group, on purpose."""

    id: str
    name: str
    member_ids: set[str] = field(default_factory=set)
    currency: str = "USD"

    def add_member(self, user_id: str) -> None:
        self.member_ids.add(user_id)

    def require_member(self, user_id: str) -> None:
        if user_id not in self.member_ids:
            raise GroupMembershipError(f"{user_id} is not a member of group {self.id}")


@dataclass(frozen=True, slots=True)
class Split:
    """One line of an expense: this user paid, or owes, this much."""

    user_id: str
    amount: Money


def total_of(splits: Iterable[Split], currency: str) -> Money:
    total = Money(0, currency)
    for split in splits:
        total = total + split.amount
    return total


@dataclass(frozen=True, slots=True)
class Expense:
    """Immutable by design: an edit produces a new version and supersedes this one.

    ``paid_by`` supports several payers, so "Alice put 200 on her card and Bob
    added 100 in cash" is one expense rather than two.
    """

    id: str
    group_id: str
    description: str
    amount: Money
    paid_by: tuple[Split, ...]
    owed_by: tuple[Split, ...]
    split_type: SplitType
    created_by: str
    created_at: float
    status: ExpenseStatus = ExpenseStatus.ACTIVE
    replaces_id: str | None = None

    def validate(self) -> None:
        """Both sides of the expense must equal the amount. This is the invariant."""
        paid = total_of(self.paid_by, self.amount.currency)
        owed = total_of(self.owed_by, self.amount.currency)
        if paid != self.amount:
            raise UnbalancedExpenseError(f"payers contributed {paid}, expense is {self.amount}")
        if owed != self.amount:
            raise UnbalancedExpenseError(f"splits add up to {owed}, expense is {self.amount}")

    def net_by_user(self) -> dict[str, Money]:
        """``paid - owed`` per participant; positive means the group owes them."""
        zero = Money(0, self.amount.currency)
        nets: dict[str, Money] = {}
        for split in self.paid_by:
            nets[split.user_id] = nets.get(split.user_id, zero) + split.amount
        for split in self.owed_by:
            nets[split.user_id] = nets.get(split.user_id, zero) - split.amount
        return {user_id: net for user_id, net in sorted(nets.items()) if not net.is_zero()}

    def with_status(self, status: ExpenseStatus) -> Expense:
        if self.status is ExpenseStatus.DELETED and status is not ExpenseStatus.ACTIVE:
            raise ExpenseStateError(f"expense {self.id} is deleted")
        if self.status is ExpenseStatus.SUPERSEDED:
            raise ExpenseStateError(f"expense {self.id} was superseded by an edit")
        return replace(self, status=status)


@dataclass(frozen=True, slots=True)
class Transfer:
    """One line of a settle-up plan: ``debtor_id`` should pay ``creditor_id``."""

    debtor_id: str
    creditor_id: str
    amount: Money


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Append-only audit row. A reversal is a row with the two ids swapped."""

    id: str
    group_id: str
    expense_id: str
    debtor_id: str
    creditor_id: str
    amount: Money
    at: float


@dataclass(frozen=True, slots=True)
class Settlement:
    id: str
    group_id: str
    payer_id: str
    payee_id: str
    amount: Money
    at: float


@dataclass(frozen=True, slots=True)
class Activity:
    """What the feed shows. Frozen, so a listener cannot corrupt the history."""

    kind: ActivityKind
    group_id: str
    actor_id: str
    subject_id: str
    summary: str
    at: float


# --8<-- [end:entities]


# --8<-- [start:balance]
@dataclass(slots=True)
class BalanceSheet:
    """Pairwise net debt inside one group, in cents.

    The key is the ordered pair ``(low_id, high_id)`` and the value is what
    ``high_id`` owes ``low_id``. Storing one direction per pair means "A owes B
    5.00" and "B owes A 5.00" cancel to nothing instead of accumulating two
    rows, and it makes ``net`` a single pass over the dict.
    """

    group_id: str
    currency: str = "USD"
    pairs: dict[tuple[str, str], int] = field(default_factory=dict)

    def record(self, debtor_id: str, creditor_id: str, amount: Money) -> None:
        """``debtor_id`` now owes ``creditor_id`` ``amount`` more than before."""
        if debtor_id == creditor_id:
            raise ValidationError("a member cannot owe themselves")
        if amount.currency != self.currency:
            raise ValidationError(f"group {self.group_id} settles in {self.currency}")
        low, high = sorted((debtor_id, creditor_id))
        delta = amount.cents if debtor_id == high else -amount.cents
        updated = self.pairs.get((low, high), 0) + delta
        if updated == 0:
            self.pairs.pop((low, high), None)
        else:
            self.pairs[(low, high)] = updated

    def settle(self, payer_id: str, payee_id: str, amount: Money) -> None:
        """Cash changed hands: the payer's debt to the payee shrinks by ``amount``."""
        self.record(payee_id, payer_id, amount)

    def between(self, user_id: str, other_id: str) -> Money:
        """How much ``user_id`` owes ``other_id`` (negative means the other way round)."""
        low, high = sorted((user_id, other_id))
        value = self.pairs.get((low, high), 0)
        return Money(value if user_id == high else -value, self.currency)

    def net(self, user_id: str) -> Money:
        """Positive means the group owes this member money."""
        cents = 0
        for (low, high), value in self.pairs.items():
            if user_id == low:
                cents += value
            elif user_id == high:
                cents -= value
        return Money(cents, self.currency)

    def nets(self, member_ids: Iterable[str]) -> dict[str, Money]:
        return {user_id: self.net(user_id) for user_id in sorted(member_ids)}

    def copy(self) -> BalanceSheet:
        return BalanceSheet(self.group_id, self.currency, dict(self.pairs))


# --8<-- [end:balance]


def splits_from(amounts: Mapping[str, Money]) -> tuple[Split, ...]:
    """Deterministic ordering: sorted by user id so two runs produce identical expenses."""
    return tuple(Split(user_id, amount) for user_id, amount in sorted(amounts.items()))
