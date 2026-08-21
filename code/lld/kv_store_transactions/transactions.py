"""The write-set chain: one savepoint per BEGIN, and the rules for reading through it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from lld.kv_store_transactions.models import Entry, TransactionConflictError

MISSING = (False, None)  # "this level says nothing about that key"


# --8<-- [start:transaction]
@dataclass(slots=True)
class Transaction:
    """One savepoint: what it wrote, and what it read from the level below.

    A delete is a tombstone - ``writes[key] = None`` - and not a removal from the
    dict. The difference is the whole trick: "deleted at this level" has to be
    distinguishable from "this level never mentioned the key", or a DELETE inside
    a transaction would fall through and return the committed value.
    """

    id: str
    writes: dict[str, Entry | None] = field(default_factory=dict)
    reads: dict[str, int] = field(default_factory=dict)  # key -> base version when first read

    def stage(self, key: str, entry: Entry | None) -> None:
        self.writes[key] = entry

    def lookup(self, key: str) -> tuple[bool, Entry | None]:
        """``(True, entry)`` or ``(True, None)`` for a tombstone; ``MISSING`` otherwise."""
        if key in self.writes:
            return True, self.writes[key]
        return MISSING

    def observe(self, key: str, version: int) -> None:
        """Record the committed version behind a read, once, for conflict detection."""
        self.reads.setdefault(key, version)

    def merge_into(self, parent: Transaction) -> None:
        """Nested COMMIT: the writes move down one level, they do not become durable.

        This is the semantic interviewers probe. After ``BEGIN; SET a 1; BEGIN;
        SET a 2; COMMIT; ROLLBACK`` the store must hold nothing: the inner commit
        only handed ``a = 2`` to its parent, and the outer rollback threw it away.
        """
        parent.writes.update(self.writes)
        for key, version in self.reads.items():
            parent.observe(key, version)


class TransactionStack:
    """A per-session stack of savepoints. Depth zero means autocommit.

    It is per session, not per store: two threads each running a transaction must
    not see each other's staged writes, and a stack shared between them would
    make ``ROLLBACK`` on one thread discard the other's work.
    """

    def __init__(self) -> None:
        self._levels: list[Transaction] = []

    def __len__(self) -> int:
        return len(self._levels)

    def push(self, transaction: Transaction) -> None:
        self._levels.append(transaction)

    def pop(self) -> Transaction:
        return self._levels.pop()

    def top(self) -> Transaction | None:
        return self._levels[-1] if self._levels else None

    def levels(self) -> list[Transaction]:
        """Outermost first. A merged view layers them in this order."""
        return list(self._levels)

    def clear(self) -> None:
        self._levels.clear()

    def lookup(self, key: str) -> tuple[bool, Entry | None]:
        """Walk from the innermost savepoint outward: the nearest write wins."""
        for level in reversed(self._levels):
            found, entry = level.lookup(key)
            if found:
                return True, entry
        return MISSING

    def staged_keys(self) -> set[str]:
        return {key for level in self._levels for key in level.writes}


# --8<-- [end:transaction]


# --8<-- [start:isolation]
class VersionSource(Protocol):
    """What an isolation policy needs from committed storage: the version of a key."""

    def version(self, key: str) -> int: ...


class IsolationPolicy(Protocol):
    """Runs at the outermost COMMIT, just before the write-set becomes durable."""

    def validate(self, transaction: Transaction, storage: VersionSource) -> None: ...


class LastWriteWins:
    """No validation: whoever commits last overwrites. Cheap, and lost updates are possible."""

    def validate(self, transaction: Transaction, storage: VersionSource) -> None:
        return None


class OptimisticIsolation:
    """Refuse the commit if any key this transaction read has changed since it read it.

    This is compare-and-set widened to a whole transaction, and it is what turns
    read-modify-write sequences such as INCR into something safe without holding
    a lock across the caller's thinking time. The cost is that a loser must redo
    its work, so it suits low-conflict workloads and not a single hot key.
    """

    def validate(self, transaction: Transaction, storage: VersionSource) -> None:
        for key, seen in transaction.reads.items():
            current = storage.version(key)
            if current != seen:
                raise TransactionConflictError(
                    f"key {key!r} changed from version {seen} to {current} during the transaction"
                )


# --8<-- [end:isolation]
