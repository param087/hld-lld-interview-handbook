"""Committed storage, the append-only log, and the store that binds them to the write-set chain."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from typing import Protocol

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock, ValidationError
from lld.kv_store_transactions.models import (
    Entry,
    KeyMissingError,
    LogEntry,
    NoTransactionError,
    Operation,
    Value,
    ValueTypeError,
)
from lld.kv_store_transactions.transactions import (
    IsolationPolicy,
    LastWriteWins,
    Transaction,
    TransactionStack,
)


# --8<-- [start:storage]
class Storage:
    """Committed state: the values, a version per key, and lazy TTL expiry.

    Versions outlive their keys. A transaction that read a key and found nothing
    still needs to notice if someone created it before the commit, so "absent"
    has to carry a version too - which it does, because ``delete`` bumps the
    counter instead of dropping it.

    Nothing here locks. The store owns the lock and holds it around every call.
    """

    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}
        self._versions: dict[str, int] = {}

    def get(self, key: str, now: float) -> Entry | None:
        """A pure read: an expired entry reports absent but is not removed here."""
        entry = self._entries.get(key)
        return None if entry is None or entry.is_expired(now) else entry

    def version(self, key: str) -> int:
        return self._versions.get(key, 0)

    def set(self, key: str, entry: Entry) -> None:
        self._entries[key] = entry
        self._versions[key] = self._versions.get(key, 0) + 1

    def delete(self, key: str) -> bool:
        if key not in self._entries:
            return False
        del self._entries[key]
        self._versions[key] = self._versions.get(key, 0) + 1
        return True

    def snapshot(self, now: float) -> dict[str, Entry]:
        return {key: entry for key, entry in self._entries.items() if not entry.is_expired(now)}

    def purge_expired(self, now: float) -> int:
        dead = [key for key, entry in self._entries.items() if entry.is_expired(now)]
        for key in dead:
            self.delete(key)
        return len(dead)

    def __len__(self) -> int:
        return len(self._entries)


class AppendOnlyLog(Protocol):
    """Durability seam. One ``append`` per commit, never one per write."""

    def append(self, records: Sequence[LogEntry]) -> None: ...

    def replay(self) -> list[LogEntry]: ...


class NullLog:
    """Null Object: the default, so the store never has to ask whether logging is on."""

    def append(self, records: Sequence[LogEntry]) -> None:
        return None

    def replay(self) -> list[LogEntry]:
        return []


class InMemoryLog:
    """Stands in for an append-only file. ``batches`` is what proves commits are atomic."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[LogEntry] = []
        self._batches = 0

    def append(self, records: Sequence[LogEntry]) -> None:
        if not records:
            return
        with self._lock:
            self._records.extend(records)
            self._batches += 1

    def replay(self) -> list[LogEntry]:
        with self._lock:
            return list(self._records)

    @property
    def batches(self) -> int:
        with self._lock:
            return self._batches


# --8<-- [end:storage]


# --8<-- [start:store]
class KVStore:
    """The store: committed storage under one lock, plus a write-set chain per session.

    Two pieces of state, and they are guarded very differently.

    * ``_storage`` is shared, so a single ``threading.RLock`` covers every read
      and write of it. One coarse lock is the honest answer for interactive
      transactions: the alternative is multi-version concurrency control, and
      claiming per-key locks would be a lie, because a commit has to apply a
      whole write-set atomically.
    * The transaction stack is *per session*, kept in ``threading.local``. It
      needs no lock at all, because exactly one thread can reach it - and it is
      the reason one thread's ``ROLLBACK`` cannot throw away another's work.
    """

    def __init__(
        self,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        isolation: IsolationPolicy | None = None,
        log: AppendOnlyLog | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("tx")
        self._isolation = isolation or LastWriteWins()
        self._log: AppendOnlyLog = log or NullLog()
        self._storage = Storage()
        self._lock = threading.RLock()
        self._sessions = threading.local()

    # -- transactions ------------------------------------------------------------
    @property
    def stack(self) -> TransactionStack:
        """This thread's savepoints. Created on first use, never shared."""
        existing = getattr(self._sessions, "stack", None)
        if existing is None:
            existing = TransactionStack()
            self._sessions.stack = existing
        return existing

    @property
    def depth(self) -> int:
        return len(self.stack)

    def begin(self) -> str:
        transaction = Transaction(self._ids.next_id())
        self.stack.push(transaction)
        return transaction.id

    def rollback(self) -> str:
        """Discard the innermost savepoint. Everything below it is untouched."""
        if not self.stack:
            raise NoTransactionError("ROLLBACK with no open transaction")
        return self.stack.pop().id

    def commit(self) -> str:
        """Inner COMMIT merges into the parent; the outermost one becomes durable."""
        if not self.stack:
            raise NoTransactionError("COMMIT with no open transaction")
        transaction = self.stack.pop()
        parent = self.stack.top()
        if parent is not None:
            transaction.merge_into(parent)
            return transaction.id
        with self._lock:
            self._isolation.validate(transaction, self._storage)  # raises, and the work is lost
            self._apply(transaction.writes)
            self._log.append([LogEntry.of(key, entry) for key, entry in transaction.writes.items()])
        return transaction.id

    # -- data --------------------------------------------------------------------
    def get(self, key: str, default: Value | None = None) -> Value | None:
        entry = self._read(key)
        return default if entry is None else entry.value

    def __getitem__(self, key: str) -> Value:
        entry = self._read(key)
        if entry is None:
            raise KeyMissingError(f"{key!r} is not in the store")
        return entry.value

    def exists(self, key: str) -> bool:
        return self._read(key) is not None

    def set(self, key: str, value: Value, ttl: float | None = None) -> None:
        if not key:
            raise ValidationError("key must be non-empty")
        if ttl is not None and ttl <= 0:
            raise ValidationError("ttl must be positive; use delete() to remove a key")
        deadline = None if ttl is None else self._clock.now() + ttl
        self._write(key, Entry(value, deadline))

    def delete(self, key: str) -> bool:
        existed = self._read(key) is not None
        self._write(key, None)
        return existed

    def incr(self, key: str, by: int = 1) -> int:
        """Read-modify-write. Outside a transaction it is one atomic step under the lock.

        Inside a transaction it cannot be: the read happens now and the write
        lands at COMMIT, so the gap between them is real. That gap is exactly
        what ``OptimisticIsolation`` exists to detect.
        """
        if self.stack.top() is None:
            with self._lock:
                entry = self._storage.get(key, self._clock.now())
                updated = Entry(_as_int(key, entry) + by, entry.expires_at if entry else None)
                self._apply({key: updated})
                self._log.append([LogEntry.of(key, updated)])
                return int(updated.value)
        entry = self._read(key)
        updated = Entry(_as_int(key, entry) + by, entry.expires_at if entry else None)
        self._write(key, updated)
        return int(updated.value)

    def decr(self, key: str, by: int = 1) -> int:
        return self.incr(key, -by)

    def scan(self, prefix: str = "") -> list[tuple[str, Value]]:
        """The merged view: committed state with every open savepoint layered on top."""
        now = self._clock.now()
        with self._lock:
            merged = self._storage.snapshot(now)
        visible = {key: entry for key, entry in merged.items() if key.startswith(prefix)}
        for level in self.stack.levels():  # outermost first, so the innermost wins
            for key, entry in level.writes.items():
                if not key.startswith(prefix):
                    continue
                if entry is None or entry.is_expired(now):
                    visible.pop(key, None)
                else:
                    visible[key] = entry
        return sorted((key, entry.value) for key, entry in visible.items())

    def count(self, value: Value) -> int:
        """How many visible keys hold ``value``. O(n) - see the page for the index variant."""
        return sum(1 for _, held in self.scan() if held == value)

    def __iter__(self) -> Iterator[tuple[str, Value]]:
        """Iterate a *snapshot*, so writing during the loop cannot corrupt it."""
        return iter(self.scan())

    def __len__(self) -> int:
        return len(self.scan())

    def purge_expired(self) -> int:
        with self._lock:
            return self._storage.purge_expired(self._clock.now())

    # -- recovery ----------------------------------------------------------------
    @classmethod
    def restore(cls, log: AppendOnlyLog, clock: Clock | None = None, **kwargs: object) -> KVStore:
        """Rebuild committed state by replaying the log, then keep writing to it."""
        store = cls(clock=clock, **kwargs)  # type: ignore[arg-type]
        for record in log.replay():
            if record.operation is Operation.DELETE:
                store._storage.delete(record.key)
            else:
                store._storage.set(record.key, Entry(record.value, record.expires_at))  # type: ignore[arg-type]
        store._log = log
        return store

    # -- internals ---------------------------------------------------------------
    def _read(self, key: str) -> Entry | None:
        """Walk the chain, then fall through to committed storage and record the version."""
        found, staged = self.stack.lookup(key)
        if found:
            if staged is None or staged.is_expired(self._clock.now()):
                return None
            return staged
        with self._lock:
            now = self._clock.now()
            entry = self._storage.get(key, now)
            version = self._storage.version(key)
        top = self.stack.top()
        if top is not None:
            top.observe(key, version)
        return entry

    def _write(self, key: str, entry: Entry | None) -> None:
        top = self.stack.top()
        if top is not None:
            top.stage(key, entry)
            return
        with self._lock:  # autocommit: one write is its own transaction
            self._apply({key: entry})
            self._log.append([LogEntry.of(key, entry)])

    def _apply(self, writes: dict[str, Entry | None]) -> None:
        """Caller holds the lock. Tombstones delete, everything else overwrites."""
        for key, entry in writes.items():
            if entry is None:
                self._storage.delete(key)
            else:
                self._storage.set(key, entry)


def _as_int(key: str, entry: Entry | None) -> int:
    """A missing key counts as 0; anything non-integer is a type error, not a coercion."""
    current = 0 if entry is None else entry.value
    if not isinstance(current, int):
        raise ValueTypeError(f"{key!r} holds {current!r}, which is not an integer")
    return current


# --8<-- [end:store]
