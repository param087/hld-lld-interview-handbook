"""The in-memory database and the Unit of Work that writes to it atomically.

Locking rule: one lock per group (``SplitwiseStore.group_lock``) is held by the
service for the whole read-modify-write of an expense; a second, very short
``_registry_lock`` inside the store guards the user and group registries only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol, Self

from lld.splitwise.models import (
    BalanceSheet,
    Expense,
    Group,
    LedgerEntry,
    Settlement,
    UnknownEntityError,
    User,
)


# --8<-- [start:store]
@dataclass(slots=True)
class GroupState:
    """Everything one group owns, and therefore the unit the Unit of Work copies."""

    group: Group
    balances: BalanceSheet
    expenses: dict[str, Expense] = field(default_factory=dict)
    ledger: list[LedgerEntry] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)

    def copy(self) -> GroupState:
        clone = Group(self.group.id, self.group.name, set(self.group.member_ids), self.group.currency)
        return GroupState(
            clone, self.balances.copy(), dict(self.expenses), list(self.ledger), list(self.settlements)
        )


class SplitwiseStore:
    """Reads hand out snapshots; writes arrive as whole, already-consistent states."""

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._users: dict[str, User] = {}
        self._groups: dict[str, GroupState] = {}
        self._group_locks: dict[str, threading.Lock] = {}

    def add_user(self, user: User) -> User:
        with self._registry_lock:
            self._users[user.id] = user
        return user

    def user(self, user_id: str) -> User:
        with self._registry_lock:
            try:
                return self._users[user_id]
            except KeyError:
                raise UnknownEntityError(f"unknown user {user_id}") from None

    def create_group(self, group: Group) -> Group:
        with self._registry_lock:
            for member_id in group.member_ids:
                if member_id not in self._users:
                    raise UnknownEntityError(f"unknown user {member_id}")
            self._groups[group.id] = GroupState(group, BalanceSheet(group.id, group.currency))
            self._group_locks[group.id] = threading.Lock()
        return group

    def group_lock(self, group_id: str) -> threading.Lock:
        with self._registry_lock:
            try:
                return self._group_locks[group_id]
            except KeyError:
                raise UnknownEntityError(f"unknown group {group_id}") from None

    def snapshot(self, group_id: str) -> GroupState:
        with self._registry_lock:
            try:
                return self._groups[group_id].copy()
            except KeyError:
                raise UnknownEntityError(f"unknown group {group_id}") from None

    def publish(self, state: GroupState) -> None:
        with self._registry_lock:
            self._groups[state.group.id] = state

    def group_ids(self) -> list[str]:
        with self._registry_lock:
            return sorted(self._groups)


# --8<-- [end:store]


# --8<-- [start:uow]
class UnitOfWork(Protocol):
    """One transaction boundary. ``state`` is the working copy you mutate."""

    state: GroupState

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class GroupUnitOfWork:
    """Expenses, ledger rows and balances of one group commit together or not at all.

    Construction copies the group's state; everything inside the ``with`` block
    mutates the copy. ``commit`` publishes that copy back into the store in a
    single assignment. Leaving the block without committing -- a failed
    validation, any raised exception -- throws the copy away, so an expense that
    moved balances but was never stored cannot exist.
    """

    def __init__(self, store: SplitwiseStore, group_id: str) -> None:
        self._store = store
        self._group_id = group_id
        self._committed = False
        self.state = store.snapshot(group_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._committed:
            self.rollback()

    def commit(self) -> None:
        self._store.publish(self.state)
        self._committed = True

    def rollback(self) -> None:
        self.state = self._store.snapshot(self._group_id)


# --8<-- [end:uow]
