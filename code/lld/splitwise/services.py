"""Debt simplification, the activity feed and the service that owns every write.

Locking rule: ``ExpenseService`` holds one lock per group
(``SplitwiseStore.group_lock``) for the whole read-modify-write of an expense.
Listeners are notified after the lock is released, so a slow feed can never
block a group.
"""

from __future__ import annotations

import heapq
import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock, ValidationError
from lld.splitwise.models import (
    Activity,
    ActivityKind,
    Expense,
    ExpenseStateError,
    ExpenseStatus,
    LedgerEntry,
    Settlement,
    SplitType,
    Transfer,
    UnknownEntityError,
    splits_from,
)
from lld.splitwise.store import GroupState, GroupUnitOfWork, SplitwiseStore
from lld.splitwise.strategies import SplitStrategyFactory


# --8<-- [start:feed]
class ActivityListener(Protocol):
    """Observer interface: anything that wants to hear about group activity."""

    def on_activity(self, activity: Activity) -> None: ...


class ActivityFeed:
    """Append-only feed, newest last. Thread-safe because several groups write to it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[Activity] = []

    def on_activity(self, activity: Activity) -> None:
        with self._lock:
            self._events.append(activity)

    def for_group(self, group_id: str) -> list[Activity]:
        with self._lock:
            return [event for event in self._events if event.group_id == group_id]

    def render(self, group_id: str, limit: int = 5) -> str:
        return "\n".join(event.summary for event in self.for_group(group_id)[-limit:])


# --8<-- [end:feed]


# --8<-- [start:simplifier]
class DebtSimplifier:
    """Greedy minimum cash flow: settle the largest debtor against the largest creditor.

    Two heaps keep the extremes at hand, so the plan costs O(n log n) and never
    needs more than n-1 transfers. Say this out loud: the result is *not*
    guaranteed to be the minimum number of transfers -- deciding that is
    NP-hard, it is subset-sum in disguise. Greedy is what production systems
    ship because "at most n-1, usually far fewer" is already a huge win.
    """

    def simplify(self, nets: Mapping[str, Money]) -> list[Transfer]:
        if not nets:
            return []
        currency = next(iter(nets.values())).currency
        if sum(net.cents for net in nets.values()) != 0:
            raise ValidationError("balances do not sum to zero; the ledger is corrupt")
        # (-cents, user_id): the heap root is the biggest amount, ties broken by id.
        creditors = [(-net.cents, user) for user, net in sorted(nets.items()) if net.cents > 0]
        debtors = [(net.cents, user) for user, net in sorted(nets.items()) if net.cents < 0]
        heapq.heapify(creditors)
        heapq.heapify(debtors)
        transfers: list[Transfer] = []
        while creditors and debtors:
            credit, creditor = heapq.heappop(creditors)
            debit, debtor = heapq.heappop(debtors)
            amount = min(-credit, -debit)
            transfers.append(Transfer(debtor, creditor, Money(amount, currency)))
            if -credit > amount:
                heapq.heappush(creditors, (credit + amount, creditor))
            if -debit > amount:
                heapq.heappush(debtors, (debit + amount, debtor))
        return transfers


# --8<-- [end:simplifier]


# --8<-- [start:service]
class ExpenseService:
    """Add, edit, delete and settle. The only writer of balances in the system.

    Every mutating method takes the group lock, opens a ``GroupUnitOfWork``,
    computes, commits, and only then notifies listeners.
    """

    def __init__(
        self,
        store: SplitwiseStore,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        listeners: Iterable[ActivityListener] = (),
        simplifier: DebtSimplifier | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("E")
        self._listeners = list(listeners)
        self._simplifier = simplifier or DebtSimplifier()

    def add_expense(
        self,
        group_id: str,
        description: str,
        paid_by: Mapping[str, Money],
        participant_ids: Sequence[str],
        split_type: SplitType | str = SplitType.EQUAL,
        weights: Sequence[int] | None = None,
        actor_id: str | None = None,
    ) -> Expense:
        with self._store.group_lock(group_id):
            with GroupUnitOfWork(self._store, group_id) as uow:
                expense = self._build(uow.state, group_id, description, paid_by, participant_ids, split_type, weights, actor_id)
                uow.state.expenses[expense.id] = expense
                self._apply(uow.state, expense, sign=1)
                uow.commit()
        self._notify(ActivityKind.EXPENSE_ADDED, expense, f"{expense.created_by} added {expense.description} for {expense.amount}")
        return expense

    def edit_expense(
        self,
        group_id: str,
        expense_id: str,
        description: str,
        paid_by: Mapping[str, Money],
        participant_ids: Sequence[str],
        split_type: SplitType | str = SplitType.EQUAL,
        weights: Sequence[int] | None = None,
        actor_id: str | None = None,
    ) -> Expense:
        """Reverse the old version, post the new one, supersede the old -- in one transaction."""
        with self._store.group_lock(group_id):
            with GroupUnitOfWork(self._store, group_id) as uow:
                old = self._active(uow.state, expense_id)
                self._apply(uow.state, old, sign=-1)
                uow.state.expenses[old.id] = old.with_status(ExpenseStatus.SUPERSEDED)
                new = self._build(uow.state, group_id, description, paid_by, participant_ids, split_type, weights, actor_id, replaces_id=old.id)
                uow.state.expenses[new.id] = new
                self._apply(uow.state, new, sign=1)
                uow.commit()
        self._notify(ActivityKind.EXPENSE_EDITED, new, f"{new.created_by} edited {new.description} to {new.amount}")
        return new

    def delete_expense(self, group_id: str, expense_id: str, actor_id: str) -> Expense:
        with self._store.group_lock(group_id):
            with GroupUnitOfWork(self._store, group_id) as uow:
                expense = self._active(uow.state, expense_id)
                self._apply(uow.state, expense, sign=-1)
                deleted = expense.with_status(ExpenseStatus.DELETED)
                uow.state.expenses[expense_id] = deleted
                uow.commit()
        self._notify(ActivityKind.EXPENSE_DELETED, deleted, f"{actor_id} deleted {deleted.description}")
        return deleted

    def restore_expense(self, group_id: str, expense_id: str, actor_id: str) -> Expense:
        """Undo of a delete: put the same version back and re-post its transfers."""
        with self._store.group_lock(group_id):
            with GroupUnitOfWork(self._store, group_id) as uow:
                expense = uow.state.expenses.get(expense_id)
                if expense is None:
                    raise UnknownEntityError(f"unknown expense {expense_id}")
                if expense.status is not ExpenseStatus.DELETED:
                    raise ExpenseStateError(f"expense {expense_id} is {expense.status}, not deleted")
                restored = expense.with_status(ExpenseStatus.ACTIVE)
                uow.state.expenses[expense_id] = restored
                self._apply(uow.state, restored, sign=1)
                uow.commit()
        self._notify(ActivityKind.EXPENSE_RESTORED, restored, f"{actor_id} restored {restored.description}")
        return restored

    def settle_up(self, group_id: str, payer_id: str, payee_id: str, amount: Money) -> Settlement:
        if amount.cents <= 0:
            raise ValidationError("a settlement must be positive")
        with self._store.group_lock(group_id):
            with GroupUnitOfWork(self._store, group_id) as uow:
                uow.state.group.require_member(payer_id)
                uow.state.group.require_member(payee_id)
                settlement = Settlement(self._ids.next_id(), group_id, payer_id, payee_id, amount, self._clock.now())
                uow.state.balances.settle(payer_id, payee_id, amount)
                uow.state.settlements.append(settlement)
                uow.commit()
        self._publish(Activity(ActivityKind.SETTLED, group_id, payer_id, settlement.id, f"{payer_id} paid {payee_id} {amount}", settlement.at))
        return settlement

    # -- queries ---------------------------------------------------------------------
    def balances(self, group_id: str) -> dict[str, Money]:
        state = self._store.snapshot(group_id)
        return state.balances.nets(state.group.member_ids)

    def balance_between(self, group_id: str, user_id: str, other_id: str) -> Money:
        return self._store.snapshot(group_id).balances.between(user_id, other_id)

    def global_balance(self, user_id: str, currency: str = "USD") -> Money:
        """Net across every group the user belongs to -- the number on the home screen."""
        total = Money(0, currency)
        for group_id in self._store.group_ids():
            state = self._store.snapshot(group_id)
            if user_id in state.group.member_ids:
                total = total + state.balances.net(user_id)
        return total

    def simplify(self, group_id: str) -> list[Transfer]:
        state = self._store.snapshot(group_id)
        nets = {u: n for u, n in state.balances.nets(state.group.member_ids).items() if not n.is_zero()}
        return self._simplifier.simplify(nets)

    def expense(self, group_id: str, expense_id: str) -> Expense:
        try:
            return self._store.snapshot(group_id).expenses[expense_id]
        except KeyError:
            raise UnknownEntityError(f"unknown expense {expense_id}") from None

    def ledger(self, group_id: str) -> list[LedgerEntry]:
        return self._store.snapshot(group_id).ledger

    # -- internals -------------------------------------------------------------------
    def _build(
        self,
        state: GroupState,
        group_id: str,
        description: str,
        paid_by: Mapping[str, Money],
        participant_ids: Sequence[str],
        split_type: SplitType | str,
        weights: Sequence[int] | None,
        actor_id: str | None,
        replaces_id: str | None = None,
    ) -> Expense:
        if not description.strip():
            raise ValidationError("an expense needs a description")
        if not paid_by:
            raise ValidationError("an expense needs at least one payer")
        for user_id in (*paid_by, *participant_ids):
            state.group.require_member(user_id)
        currency = state.group.currency
        total = Money(0, currency)
        for amount in paid_by.values():
            total = total + amount
        if total.cents <= 0:
            raise ValidationError("an expense must be positive")
        strategy = SplitStrategyFactory.create(split_type)
        owed = strategy.split(total, list(participant_ids), weights)
        expense = Expense(
            id=self._ids.next_id(),
            group_id=group_id,
            description=description.strip(),
            amount=total,
            paid_by=splits_from(paid_by),
            owed_by=tuple(owed),
            split_type=SplitType(split_type),
            created_by=actor_id or next(iter(sorted(paid_by))),
            created_at=self._clock.now(),
            replaces_id=replaces_id,
        )
        expense.validate()
        return expense

    @staticmethod
    def _active(state: GroupState, expense_id: str) -> Expense:
        expense = state.expenses.get(expense_id)
        if expense is None:
            raise UnknownEntityError(f"unknown expense {expense_id}")
        if expense.status is not ExpenseStatus.ACTIVE:
            raise ExpenseStateError(f"expense {expense_id} is {expense.status}")
        return expense

    def _apply(self, state: GroupState, expense: Expense, sign: int) -> None:
        """Post (sign=+1) or reverse (sign=-1) an expense against the balance sheet.

        The transfers are re-derived from the expense, and ``DebtSimplifier`` is
        deterministic, so a reversal cancels the original exactly -- no need to
        look the old ledger rows up.
        """
        at = self._clock.now()
        for transfer in self._simplifier.simplify(expense.net_by_user()):
            debtor, creditor = (transfer.debtor_id, transfer.creditor_id) if sign > 0 else (transfer.creditor_id, transfer.debtor_id)
            state.balances.record(debtor, creditor, transfer.amount)
            state.ledger.append(
                LedgerEntry(self._ids.next_id(), expense.group_id, expense.id, debtor, creditor, transfer.amount, at)
            )

    def _notify(self, kind: ActivityKind, expense: Expense, summary: str) -> None:
        self._publish(Activity(kind, expense.group_id, expense.created_by, expense.id, summary, expense.created_at))

    def _publish(self, activity: Activity) -> None:
        # Outside every lock: a slow listener must not stall the group.
        for listener in self._listeners:
            listener.on_activity(activity)


# --8<-- [end:service]
