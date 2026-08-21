"""Event sourcing and CQRS on a bank account: events, replay, snapshots, projections.

What the module demonstrates, in the order an interviewer asks about it:

* ``BankAccount`` is an aggregate. Commands (``deposit``, ``withdraw``) validate invariants and
  *raise events*; ``_apply`` is the only code that changes state, so replaying the events
  rebuilds the same aggregate. Current state is a fold over the history, never stored as such.
* ``EventStore`` holds one append-only stream per aggregate with optimistic concurrency: an
  append names the version it expects, and a stale writer gets ``ConflictError`` instead of
  silently overwriting. Every event also gets a global position, which is what projections
  checkpoint.
* ``AccountRepository`` loads an aggregate from its latest snapshot plus the events after it,
  so a stream with thousands of events costs a handful of reads.
* ``Projection`` subclasses are the query side of CQRS: read models built by consuming the
  global log, idempotent on redelivery, and rebuildable from scratch when a new question
  arrives that the existing models cannot answer.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from common import (
    Clock,
    ConflictError,
    FakeClock,
    InvalidStateError,
    Money,
    NotFoundError,
    ValidationError,
)


# --8<-- [start:events]
@dataclass(frozen=True, slots=True)
class AccountOpened:
    owner: str


@dataclass(frozen=True, slots=True)
class MoneyDeposited:
    amount: Money


@dataclass(frozen=True, slots=True)
class MoneyWithdrawn:
    amount: Money


@dataclass(frozen=True, slots=True)
class AccountClosed:
    reason: str


type DomainEvent = AccountOpened | MoneyDeposited | MoneyWithdrawn | AccountClosed


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """An event as the store keeps it: stream-local version plus a global position."""

    stream_id: str
    version: int  # 1-based position inside the stream
    position: int  # 1-based position in the global log, the projections' checkpoint
    timestamp: float
    event: DomainEvent


def describe(event: DomainEvent) -> str:
    match event:
        case AccountOpened(owner=owner):
            return f"Opened({owner})"
        case MoneyDeposited(amount=amount):
            return f"Deposited({amount.cents / 100:.2f})"
        case MoneyWithdrawn(amount=amount):
            return f"Withdrawn({amount.cents / 100:.2f})"
        case AccountClosed(reason=reason):
            return f"Closed({reason})"


# --8<-- [end:events]


# --8<-- [start:aggregate]
@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """The aggregate's state at ``version``; loading starts here instead of at event 1."""

    account_id: str
    version: int
    owner: str
    balance: Money
    closed: bool


@dataclass(slots=True)
class BankAccount:
    """The aggregate. Commands validate and raise events; ``_apply`` only mutates state.

    ``version`` counts applied events, so after loading it equals the stream version, which
    is the number the repository hands to the store as the expected version on save.
    """

    account_id: str
    owner: str = ""
    balance: Money = field(default_factory=lambda: Money(0))
    closed: bool = False
    version: int = 0
    _pending: list[DomainEvent] = field(default_factory=list)

    # -- commands: validate the invariant, then raise the event --------------------------
    @classmethod
    def open(cls, account_id: str, owner: str) -> BankAccount:
        if not account_id or not owner:
            raise ValidationError("account id and owner must be non-empty")
        account = cls(account_id)
        account._raise(AccountOpened(owner))
        return account

    def deposit(self, amount: Money) -> None:
        self._require_open()
        self._require_positive(amount)
        self._raise(MoneyDeposited(amount))

    def withdraw(self, amount: Money) -> None:
        self._require_open()
        self._require_positive(amount)
        if amount > self.balance:
            raise InvalidStateError(f"insufficient funds: balance {self.balance}")
        self._raise(MoneyWithdrawn(amount))

    def close(self, reason: str) -> None:
        self._require_open()
        if not self.balance.is_zero():
            raise InvalidStateError(f"close requires a zero balance, have {self.balance}")
        self._raise(AccountClosed(reason))

    def _require_open(self) -> None:
        if self.version == 0:
            raise InvalidStateError(f"account {self.account_id!r} does not exist")
        if self.closed:
            raise InvalidStateError(f"account {self.account_id!r} is closed")

    @staticmethod
    def _require_positive(amount: Money) -> None:
        if amount.cents <= 0:
            raise ValidationError("amount must be positive")

    # -- events: the only way state changes ----------------------------------------------
    def _raise(self, event: DomainEvent) -> None:
        self._apply(event)
        self._pending.append(event)

    def _apply(self, event: DomainEvent) -> None:
        match event:
            case AccountOpened(owner=owner):
                self.owner = owner
            case MoneyDeposited(amount=amount):
                self.balance = self.balance + amount
            case MoneyWithdrawn(amount=amount):
                self.balance = self.balance - amount
            case AccountClosed():
                self.closed = True
        self.version += 1

    @classmethod
    def replay(
        cls,
        account_id: str,
        events: Iterable[DomainEvent],
        snapshot: AccountSnapshot | None = None,
    ) -> BankAccount:
        """Rebuild the aggregate: start from the snapshot (if any), fold the events after it."""
        account = cls(account_id)
        if snapshot is not None:
            if snapshot.account_id != account_id:
                raise ValidationError("snapshot belongs to another account")
            account.owner, account.balance = snapshot.owner, snapshot.balance
            account.closed, account.version = snapshot.closed, snapshot.version
        for event in events:
            account._apply(event)
        return account

    @property
    def pending_events(self) -> tuple[DomainEvent, ...]:
        return tuple(self._pending)

    def mark_committed(self) -> None:
        self._pending.clear()

    def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(self.account_id, self.version, self.owner, self.balance, self.closed)


# --8<-- [end:aggregate]


# --8<-- [start:store]
class EventStore:
    """Append-only streams with optimistic concurrency and a global log.

    ``_lock`` guards ``_streams``, ``_log`` and ``_snapshots``. The check "is the stream still
    at the version the writer saw?" and the append happen under the same lock, which is the
    whole point: two writers that loaded version 4 cannot both append version 5.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or FakeClock()
        self._streams: dict[str, list[StoredEvent]] = {}
        self._log: list[StoredEvent] = []
        self._snapshots: dict[str, AccountSnapshot] = {}
        self._lock = threading.Lock()

    def append(
        self, stream_id: str, events: Sequence[DomainEvent], expected_version: int
    ) -> list[StoredEvent]:
        """Append ``events`` if the stream is at ``expected_version``; ``ConflictError`` if not."""
        if not events:
            raise ValidationError("nothing to append")
        if expected_version < 0:
            raise ValidationError("expected_version must be >= 0")
        with self._lock:
            stream = self._streams.setdefault(stream_id, [])
            if len(stream) != expected_version:
                raise ConflictError(
                    f"stream {stream_id!r} is at version {len(stream)}, expected {expected_version}"
                )
            now = self._clock.now()
            stored: list[StoredEvent] = []
            for offset, event in enumerate(events, start=1):
                record = StoredEvent(
                    stream_id, expected_version + offset, len(self._log) + 1, now, event
                )
                stream.append(record)
                self._log.append(record)
                stored.append(record)
            return stored

    def load(self, stream_id: str, after_version: int = 0) -> list[StoredEvent]:
        with self._lock:
            return self._streams.get(stream_id, [])[after_version:]

    def version(self, stream_id: str) -> int:
        with self._lock:
            return len(self._streams.get(stream_id, []))

    def read_all(self, after_position: int = 0) -> list[StoredEvent]:
        """The global log after ``after_position``: what a projection subscribes to."""
        with self._lock:
            return self._log[after_position:]

    def save_snapshot(self, snapshot: AccountSnapshot) -> None:
        with self._lock:
            current = self._snapshots.get(snapshot.account_id)
            if current is None or snapshot.version > current.version:
                self._snapshots[snapshot.account_id] = snapshot

    def snapshot(self, stream_id: str) -> AccountSnapshot | None:
        with self._lock:
            return self._snapshots.get(stream_id)


class AccountRepository:
    """Load = snapshot + events after it; save = append with the expected version."""

    def __init__(self, store: EventStore, snapshot_every: int = 100) -> None:
        if snapshot_every <= 0:
            raise ValidationError("snapshot_every must be positive")
        self._store = store
        self._snapshot_every = snapshot_every

    def load(self, account_id: str) -> BankAccount:
        snapshot = self._store.snapshot(account_id)
        after = snapshot.version if snapshot else 0
        events = self._store.load(account_id, after_version=after)
        if snapshot is None and not events:
            raise NotFoundError(f"account {account_id!r} does not exist")
        return BankAccount.replay(account_id, (e.event for e in events), snapshot)

    def save(self, account: BankAccount) -> list[StoredEvent]:
        pending = account.pending_events
        if not pending:
            return []
        expected = account.version - len(pending)
        stored = self._store.append(account.account_id, pending, expected_version=expected)
        account.mark_committed()
        last = self._store.snapshot(account.account_id)
        if account.version - (last.version if last else 0) >= self._snapshot_every:
            self._store.save_snapshot(account.snapshot())
        return stored


# --8<-- [end:store]


# --8<-- [start:projections]
class Projection(ABC):
    """A read model fed by the global log. One projector thread owns it (like one member of a
    consumer group), so it needs no lock; ``checkpoint`` makes redelivery harmless."""

    def __init__(self) -> None:
        self.checkpoint = 0

    def apply(self, stored: StoredEvent) -> bool:
        """Apply one event; ``False`` if it was already applied (at-least-once delivery)."""
        if stored.position <= self.checkpoint:
            return False
        self._handle(stored)
        self.checkpoint = stored.position
        return True

    def catch_up(self, store: EventStore) -> int:
        return sum(self.apply(stored) for stored in store.read_all(after_position=self.checkpoint))

    def rebuild(self, store: EventStore) -> int:
        """Throw the read model away and replay the full history: how a new model is born."""
        self._reset()
        self.checkpoint = 0
        return self.catch_up(store)

    @abstractmethod
    def _handle(self, stored: StoredEvent) -> None: ...

    @abstractmethod
    def _reset(self) -> None: ...


@dataclass(slots=True)
class AccountSummary:
    owner: str
    balance: Money = field(default_factory=lambda: Money(0))
    deposits: int = 0
    withdrawals: int = 0
    closed: bool = False


class AccountSummaryProjection(Projection):
    """One row per account, shaped for the query side: no replay needed to read a balance."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, AccountSummary] = {}

    def _reset(self) -> None:
        self.rows = {}

    def _handle(self, stored: StoredEvent) -> None:
        match stored.event:
            case AccountOpened(owner=owner):
                self.rows[stored.stream_id] = AccountSummary(owner)
            case MoneyDeposited(amount=amount):
                row = self.rows[stored.stream_id]
                row.balance, row.deposits = row.balance + amount, row.deposits + 1
            case MoneyWithdrawn(amount=amount):
                row = self.rows[stored.stream_id]
                row.balance, row.withdrawals = row.balance - amount, row.withdrawals + 1
            case AccountClosed():
                self.rows[stored.stream_id].closed = True

    def top_balances(self, limit: int = 3) -> list[tuple[str, Money]]:
        ranked = sorted(self.rows.items(), key=lambda kv: (-kv[1].balance.cents, kv[0]))
        return [(account_id, row.balance) for account_id, row in ranked[:limit]]


class LargeMovementsProjection(Projection):
    """An audit model nobody asked for at design time, built later from the same history."""

    def __init__(self, threshold: Money) -> None:
        super().__init__()
        self._threshold = threshold
        self.movements: list[tuple[str, str, Money]] = []  # (account, kind, amount)

    def _reset(self) -> None:
        self.movements = []

    def _handle(self, stored: StoredEvent) -> None:
        match stored.event:
            case MoneyDeposited(amount=amount) if amount >= self._threshold:
                self.movements.append((stored.stream_id, "deposit", amount))
            case MoneyWithdrawn(amount=amount) if amount >= self._threshold:
                self.movements.append((stored.stream_id, "withdrawal", amount))


# --8<-- [end:projections]


def main() -> None:
    store = EventStore(FakeClock(start=1_700_000_000.0))
    repo = AccountRepository(store, snapshot_every=50)

    ann = BankAccount.open("acc-1", "ann")
    ann.deposit(Money.of("100.00"))
    ann.withdraw(Money.of("30.00"))
    ann.deposit(Money.of("12.50"))
    stored = repo.save(ann)
    print(f"acc-1: {len(stored)} events appended, balance {ann.balance}, version {ann.version}")
    print("stream acc-1: " + " ".join(f"v{e.version}={describe(e.event)}" for e in stored))
    try:
        ann.withdraw(Money.of("500.00"))
    except InvalidStateError as exc:
        print(f"withdraw 500.00 rejected ({exc}); nothing appended, version {ann.version}")

    first, second = repo.load("acc-1"), repo.load("acc-1")
    first.deposit(Money.of("1.00"))
    repo.save(first)
    second.deposit(Money.of("2.00"))
    try:
        repo.save(second)
    except ConflictError as exc:
        print(f"optimistic concurrency: {exc}")

    history = [e.event for e in store.load("acc-1")]
    rebuilt = BankAccount.replay("acc-1", history)
    print(
        f"replay {len(history)} events from scratch: balance {rebuilt.balance}, "
        f"version {rebuilt.version}, same as the live aggregate: {rebuilt == repo.load('acc-1')}"
    )

    repo.save(BankAccount.open("acc-2", "bob"))
    for _ in range(120):
        bob = repo.load("acc-2")
        bob.deposit(Money.of("1.00"))
        repo.save(bob)
    snap = store.snapshot("acc-2")
    assert snap is not None
    tail = len(store.load("acc-2", after_version=snap.version))
    print(
        f"acc-2 after {store.version('acc-2')} events: snapshot at v{snap.version}, "
        f"load replays {tail} events instead of {store.version('acc-2')}"
    )

    summary = AccountSummaryProjection()
    consumed = summary.catch_up(store)
    rows = ", ".join(f"{acc}={row.balance}" for acc, row in sorted(summary.rows.items()))
    print(f"summary projection: {consumed} events up to position {summary.checkpoint}: {rows}")
    redelivered = store.read_all(after_position=summary.checkpoint - 3)
    applied = sum(summary.apply(e) for e in redelivered)
    print(
        f"redeliver the last {len(redelivered)} events: {applied} applied (the checkpoint skips them)"
    )

    carol = BankAccount.open("acc-3", "carol")
    carol.deposit(Money.of("75.00"))
    carol.withdraw(Money.of("75.00"))
    carol.close("moved abroad")
    repo.save(carol)
    fresh = summary.catch_up(store)
    top = ", ".join(f"{acc}={balance}" for acc, balance in summary.top_balances(2))
    print(f"acc-3: opened, moved 75.00 twice and closed: {fresh} new events; top balances {top}")

    audit = LargeMovementsProjection(threshold=Money.of("50.00"))
    audit.rebuild(store)
    moves = " ".join(f"{acc}:{kind}:{amount}" for acc, kind, amount in audit.movements)
    print(f"new audit projection from {audit.checkpoint} historical events: {moves}")


if __name__ == "__main__":
    main()
