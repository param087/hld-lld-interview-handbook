"""Double-entry ledger, an idempotent payment state machine and a reconciliation diff.

What the module demonstrates, in the order an interviewer asks about it:

* ``Ledger.post`` writes a balanced, immutable transaction: the signed amounts of its entries
  sum to zero, so the trial balance is zero after every write. Nothing is updated in place; a
  mistake is corrected by posting its reversal.
* ``post`` is keyed by an idempotency key, so a retried request returns the transaction written
  the first time instead of moving the money twice.
* Accounts carry a ``version``. Passing ``expected_versions`` turns a post into the twin of
  ``UPDATE account SET balance = ?, version = version + 1 WHERE id = ? AND version = ?`` plus a
  row-count check: the optimistic lock a hot wallet needs.
* ``PaymentStateMachine.apply`` drives the lifecycle from provider webhooks that arrive
  duplicated or out of order. Stale events are ignored; impossible jumps raise.
* ``reconcile`` diffs the ledger against the provider's settlement file into three buckets.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from common import (
    Clock,
    ConflictError,
    IdGenerator,
    InvalidStateError,
    Money,
    NotFoundError,
    SequentialIdGenerator,
    SystemClock,
    ValidationError,
)


# --8<-- [start:models]
class AccountType(StrEnum):
    ASSET = "asset"  # cash held at the provider, receivables
    LIABILITY = "liability"  # customer wallet balances: the platform owes them
    REVENUE = "revenue"  # fees earned
    EXPENSE = "expense"  # provider costs, chargebacks

    @property
    def normal_is_debit(self) -> bool:
        """Assets and expenses grow with debits; liabilities and revenue grow with credits."""
        return self in (AccountType.ASSET, AccountType.EXPENSE)


@dataclass(slots=True)
class Account:
    account_id: str
    type: AccountType
    currency: str = "USD"
    allow_negative: bool = False  # a wallet may not go overdrawn; a provider cash account may
    version: int = 0  # optimistic lock, bumped by every posting that touches the account
    signed_cents: int = 0  # debits minus credits, the only number the ledger stores

    def balance(self) -> Money:
        """Balance in its natural sign: a wallet with 50.00 in it reads +50.00."""
        cents = self.signed_cents if self.type.normal_is_debit else -self.signed_cents
        return Money(cents, self.currency)


@dataclass(frozen=True, slots=True)
class Entry:
    """One line of a transaction. Positive is a debit, negative is a credit."""

    account_id: str
    amount: Money

    @staticmethod
    def debit(account_id: str, amount: Money) -> Entry:
        if amount.cents <= 0:
            raise ValidationError("a debit must be a positive amount")
        return Entry(account_id, amount)

    @staticmethod
    def credit(account_id: str, amount: Money) -> Entry:
        if amount.cents <= 0:
            raise ValidationError("a credit must be a positive amount")
        return Entry(account_id, -amount)


@dataclass(frozen=True, slots=True)
class LedgerTransaction:
    txn_id: str
    idempotency_key: str
    entries: tuple[Entry, ...]
    memo: str
    posted_at: float


# --8<-- [end:models]


# --8<-- [start:ledger]
class Ledger:
    """Append-only double-entry ledger. ``_lock`` guards ``_accounts``, ``_txns`` and ``_by_key``.

    In production each ``post`` is one database transaction: insert the transaction row, insert
    its entry rows, and update the touched account balances under the same commit. The unique
    index on ``idempotency_key`` is what makes a retry a no-op instead of a second payment.
    """

    def __init__(self, clock: Clock | None = None, ids: IdGenerator | None = None) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("txn")
        self._accounts: dict[str, Account] = {}
        self._txns: list[LedgerTransaction] = []
        self._by_key: dict[str, LedgerTransaction] = {}
        self._lock = threading.Lock()

    def open_account(
        self, account_id: str, type: AccountType, currency: str = "USD", allow_negative: bool = False
    ) -> Account:
        with self._lock:
            if account_id in self._accounts:
                raise ConflictError(f"account {account_id} already exists")
            account = Account(account_id, type, currency, allow_negative)
            self._accounts[account_id] = account
            return account

    def post(
        self,
        idempotency_key: str,
        entries: Sequence[Entry],
        memo: str = "",
        expected_versions: Mapping[str, int] | None = None,
    ) -> LedgerTransaction:
        """Write one balanced transaction, or nothing at all."""
        if len(entries) < 2:
            raise ValidationError("a double-entry transaction needs at least two entries")
        currencies = {entry.amount.currency for entry in entries}
        if len(currencies) != 1:
            raise ValidationError(f"one transaction, one currency: got {sorted(currencies)}")
        total = sum(entry.amount.cents for entry in entries)
        if total != 0:
            raise ValidationError(f"transaction does not balance: debits minus credits is {total}")
        with self._lock:
            replay = self._by_key.get(idempotency_key)
            if replay is not None:
                return replay  # a retried request: the money moved exactly once
            touched = self._resolve(entries, currencies.pop())
            self._check_versions(touched, expected_versions)
            self._check_funds(entries, touched)
            for entry in entries:
                account = touched[entry.account_id]
                account.signed_cents += entry.amount.cents
            for account in touched.values():
                account.version += 1
            txn = LedgerTransaction(
                self._ids.next_id(), idempotency_key, tuple(entries), memo, self._clock.now()
            )
            self._txns.append(txn)
            self._by_key[idempotency_key] = txn
            return txn

    def _resolve(self, entries: Sequence[Entry], currency: str) -> dict[str, Account]:
        touched: dict[str, Account] = {}
        for entry in entries:
            account = self._accounts.get(entry.account_id)
            if account is None:
                raise NotFoundError(f"unknown account {entry.account_id}")
            if account.currency != currency:
                raise ValidationError(f"account {account.account_id} is not in {currency}")
            touched[account.account_id] = account
        return touched

    @staticmethod
    def _check_versions(touched: Mapping[str, Account], expected: Mapping[str, int] | None) -> None:
        if expected is None:
            return
        stale = [
            account_id
            for account_id, version in expected.items()
            if account_id in touched and touched[account_id].version != version
        ]
        if stale:  # rowcount 0 on the conditional UPDATE: somebody moved the balance first
            raise ConflictError(f"stale account version for {stale}; re-read and retry")

    @staticmethod
    def _check_funds(entries: Sequence[Entry], touched: Mapping[str, Account]) -> None:
        after: dict[str, int] = {}
        for entry in entries:
            after[entry.account_id] = after.get(entry.account_id, 0) + entry.amount.cents
        for account_id, delta in after.items():
            account = touched[account_id]
            if account.allow_negative:
                continue
            signed = account.signed_cents + delta
            natural = signed if account.type.normal_is_debit else -signed
            if natural < 0:
                raise ConflictError(f"{account_id} would go to {Money(natural, account.currency)}")

    # -- read path ---------------------------------------------------------------------------
    def balance(self, account_id: str) -> Money:
        with self._lock:
            if account_id not in self._accounts:
                raise NotFoundError(f"unknown account {account_id}")
            return self._accounts[account_id].balance()

    def version(self, account_id: str) -> int:
        with self._lock:
            if account_id not in self._accounts:
                raise NotFoundError(f"unknown account {account_id}")
            return self._accounts[account_id].version

    def trial_balance(self) -> int:
        """Sum of every signed balance. It is zero, always, or the ledger is broken."""
        with self._lock:
            return sum(account.signed_cents for account in self._accounts.values())

    def statement(self, account_id: str) -> list[tuple[str, Money]]:
        """Every entry that touched the account, oldest first: the customer-visible statement."""
        with self._lock:
            return [
                (txn.txn_id, entry.amount)
                for txn in self._txns
                for entry in txn.entries
                if entry.account_id == account_id
            ]


# --8<-- [end:ledger]


# --8<-- [start:state_machine]
class PaymentState(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"  # funds reserved on the card, nothing moved yet
    CAPTURED = "captured"  # the provider took the money
    SETTLED = "settled"  # the money reached our bank account, per the settlement file
    FAILED = "failed"
    REFUNDED = "refunded"


# How far along the happy path each state is. An event that does not move forward is stale.
_RANK: dict[PaymentState, int] = {
    PaymentState.CREATED: 0,
    PaymentState.AUTHORIZED: 1,
    PaymentState.CAPTURED: 2,
    # A declined capture must still be able to fail an authorized payment, so FAILED ranks
    # above AUTHORIZED; ranking it level with CAPTURED is what makes a *late* failure stale.
    PaymentState.FAILED: 2,
    PaymentState.SETTLED: 3,
    PaymentState.REFUNDED: 4,
}
_ALLOWED: dict[PaymentState, frozenset[PaymentState]] = {
    PaymentState.CREATED: frozenset({PaymentState.AUTHORIZED, PaymentState.FAILED}),
    PaymentState.AUTHORIZED: frozenset({PaymentState.CAPTURED, PaymentState.FAILED}),
    PaymentState.CAPTURED: frozenset({PaymentState.SETTLED, PaymentState.REFUNDED}),
    PaymentState.SETTLED: frozenset({PaymentState.REFUNDED}),
    PaymentState.FAILED: frozenset(),
    PaymentState.REFUNDED: frozenset(),
}


@dataclass(slots=True)
class Payment:
    payment_id: str
    amount: Money
    state: PaymentState = PaymentState.CREATED
    applied_events: set[str] = field(default_factory=set)  # provider event ids already seen


class PaymentStateMachine:
    """Applies provider webhooks to a payment. Duplicates and stale events are dropped."""

    def apply(self, payment: Payment, event_id: str, target: PaymentState) -> bool:
        """Return True if the payment moved. ``_lock``-free: callers hold the row lock."""
        if event_id in payment.applied_events:
            return False  # the provider retried the same webhook
        if _RANK[target] <= _RANK[payment.state]:
            payment.applied_events.add(event_id)
            return False  # authorized arriving after captured: already implied, ignore it
        if target not in _ALLOWED[payment.state]:
            # Deliberately *not* recorded as applied: an impossible event is an alert, and the
            # same event redelivered once the payment has moved on may well be legitimate.
            raise InvalidStateError(f"cannot go {payment.state} -> {target} for {payment.payment_id}")
        payment.applied_events.add(event_id)
        payment.state = target
        return True


# --8<-- [end:state_machine]


# --8<-- [start:reconcile]
@dataclass(frozen=True, slots=True)
class SettlementRow:
    """One line of the provider's daily settlement file."""

    payment_id: str
    amount: Money


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    matched: tuple[str, ...]
    missing_in_ledger: tuple[str, ...]  # they settled it, we never booked it: post a correction
    missing_at_provider: tuple[str, ...]  # we booked it, they never settled: chase or reverse
    amount_mismatch: tuple[tuple[str, Money, Money], ...]  # id, ours, theirs

    @property
    def is_clean(self) -> bool:
        return not (self.missing_in_ledger or self.missing_at_provider or self.amount_mismatch)

    def summary(self) -> str:
        return (
            f"matched={len(self.matched)} missing_in_ledger={len(self.missing_in_ledger)} "
            f"missing_at_provider={len(self.missing_at_provider)} "
            f"amount_mismatch={len(self.amount_mismatch)}"
        )


def reconcile(ours: Mapping[str, Money], theirs: Iterable[SettlementRow]) -> ReconciliationReport:
    """Diff our captured payments against the provider's settlement file, in one pass."""
    theirs_by_id = {row.payment_id: row.amount for row in theirs}
    matched: list[str] = []
    mismatch: list[tuple[str, Money, Money]] = []
    for payment_id, amount in sorted(ours.items()):
        other = theirs_by_id.get(payment_id)
        if other is None:
            continue
        if other == amount:
            matched.append(payment_id)
        else:
            mismatch.append((payment_id, amount, other))
    return ReconciliationReport(
        matched=tuple(matched),
        missing_in_ledger=tuple(sorted(set(theirs_by_id) - set(ours))),
        missing_at_provider=tuple(sorted(set(ours) - set(theirs_by_id))),
        amount_mismatch=tuple(mismatch),
    )


# --8<-- [end:reconcile]


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=1_700_000_000)
    ledger = Ledger(clock, SequentialIdGenerator("txn"))
    ledger.open_account("cash:provider", AccountType.ASSET, allow_negative=True)
    ledger.open_account("wallet:ann", AccountType.LIABILITY)
    ledger.open_account("wallet:bob", AccountType.LIABILITY)
    ledger.open_account("revenue:fees", AccountType.REVENUE)

    top_up = Money.of("50.00")
    lines = [Entry.debit("cash:provider", top_up), Entry.credit("wallet:ann", top_up)]
    ledger.post("topup-ann-1", lines, memo="card top-up")
    print(f"ann tops up {top_up}                -> wallet:ann {ledger.balance('wallet:ann')}")
    replay = ledger.post("topup-ann-1", lines)
    print(f"the client retries the same key    -> replayed {replay.txn_id}, wallet:ann still {ledger.balance('wallet:ann')}")

    amount = Money.of("12.34")
    fee = amount * Decimal("0.02")  # 2% platform fee, rounded half-up to whole cents
    net = amount - fee
    version = ledger.version("wallet:ann")
    split = [Entry.debit("wallet:ann", amount), Entry.credit("wallet:bob", net), Entry.credit("revenue:fees", fee)]
    ledger.post("transfer-1", split, memo="2% fee", expected_versions={"wallet:ann": version})
    print(f"ann sends {amount} to bob, fee {fee} -> ann {ledger.balance('wallet:ann')}, bob {ledger.balance('wallet:bob')}, fees {ledger.balance('revenue:fees')}")
    retry = [Entry.debit("wallet:ann", amount), Entry.credit("wallet:bob", amount)]
    try:
        ledger.post("transfer-2", retry, expected_versions={"wallet:ann": version})
    except ConflictError as exc:
        print(f"a concurrent writer uses version {version}  -> rejected: {exc}")
    big = Money.of("999.00")
    try:
        ledger.post("transfer-3", [Entry.debit("wallet:bob", big), Entry.credit("wallet:ann", big)])
    except ConflictError as exc:
        print(f"bob tries to send 999.00           -> rejected: {exc}")
    print(f"trial balance after 2 postings     -> {ledger.trial_balance()} (debits equal credits)")

    machine, payment = PaymentStateMachine(), Payment("pay-1", amount)
    machine.apply(payment, "evt-1", PaymentState.AUTHORIZED)
    machine.apply(payment, "evt-2", PaymentState.CAPTURED)
    duplicate = machine.apply(payment, "evt-2", PaymentState.CAPTURED)
    late = machine.apply(payment, "evt-3", PaymentState.AUTHORIZED)
    stale_failure = machine.apply(payment, "evt-4", PaymentState.FAILED)
    print(f"webhooks authorized, captured      -> {payment.state}; duplicate={duplicate}, late authorize={late}, late failure={stale_failure}")
    try:
        machine.apply(Payment("pay-2", amount), "evt-5", PaymentState.SETTLED)
    except InvalidStateError as exc:
        print(f"settled arrives before authorized  -> rejected: {exc}")

    ours = {"pay-1": Money.of("12.34"), "pay-2": Money.of("40.00"), "pay-3": Money.of("7.50")}
    rows = ["pay-1 12.34", "pay-2 39.10", "pay-9 5.00"]
    theirs = [SettlementRow(pid, Money.of(amt)) for pid, amt in (r.split() for r in rows)]
    report = reconcile(ours, theirs)
    print(f"reconcile 3 payments vs 3 rows     -> {report.summary()}")
    for payment_id, mine, other in report.amount_mismatch:
        print(f"  {payment_id}: ledger {mine} vs provider {other} -> investigate the fee split")
    print(f"  never settled: {list(report.missing_at_provider)}; unknown to us: {list(report.missing_in_ledger)}")


if __name__ == "__main__":
    main()
