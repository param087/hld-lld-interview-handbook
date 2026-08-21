"""Unit of Work: one transaction boundary over several repositories.

The running example is a money transfer between two wallet accounts: debit one,
credit the other, append a ledger entry. Either all three writes become visible
or none does. ``TransferService`` opens *a* ``UnitOfWork`` with ``with``, writes
through its repositories and calls ``commit()`` once. ``SqliteUnitOfWork`` hands
the bookkeeping to the database transaction (BEGIN, COMMIT, ROLLBACK);
``InMemoryUnitOfWork`` tracks changes in a working copy that ``commit`` publishes,
so the fake is as atomic as the real thing and a test can prove the rollback.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Protocol, Self

from common import (
    Clock,
    ConflictError,
    FakeClock,
    IdGenerator,
    InvalidStateError,
    Money,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)


# --8<-- [start:model]
class InsufficientFundsError(InvalidStateError):
    """The source account cannot cover the transfer."""


@dataclass(frozen=True, slots=True)
class Account:
    """A wallet. Frozen: a debit or a credit is a new value handed to ``save``."""

    id: str
    balance: Money

    def debit(self, amount: Money) -> Account:
        if amount > self.balance:
            raise InsufficientFundsError(f"{self.id} holds {self.balance}, cannot debit {amount}")
        return replace(self, balance=self.balance - amount)

    def credit(self, amount: Money) -> Account:
        return replace(self, balance=self.balance + amount)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Append-only record of one transfer; its id doubles as the idempotency key."""

    id: str
    source_id: str
    target_id: str
    amount: Money
    at: float


class AccountRepository(Protocol):
    def get(self, account_id: str) -> Account: ...  # NotFoundError when absent

    def save(self, account: Account) -> None: ...  # insert or replace


class LedgerRepository(Protocol):
    def append(self, entry: LedgerEntry) -> None: ...  # ConflictError on a reused id

    def for_account(self, account_id: str) -> list[LedgerEntry]: ...


class UnitOfWork(Protocol):
    """The boundary: repositories that share one transaction, plus commit and rollback.

    ``commit()`` publishes everything written so far, ``rollback()`` discards it, and
    both leave the block open. ``__exit__`` always rolls back, so leaving without
    ``commit()`` loses the work instead of writing half of it. The repositories exist
    only inside the block. Not reentrant: nesting is a bug here, not a savepoint.
    """

    accounts: AccountRepository
    ledger: LedgerRepository

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc_info: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


# --8<-- [end:model]


# --8<-- [start:sqlite]
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id       TEXT PRIMARY KEY,
    cents    INTEGER NOT NULL,
    currency TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id        TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES accounts(id),
    target_id TEXT NOT NULL REFERENCES accounts(id),
    cents     INTEGER NOT NULL,
    currency  TEXT NOT NULL,
    at        REAL NOT NULL
);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """``autocommit=True`` switches off sqlite3's implicit transactions, so the Unit of
    Work spells BEGIN, COMMIT and ROLLBACK itself and the boundary is visible in one class."""
    conn = sqlite3.connect(path, autocommit=True)
    conn.executescript(SCHEMA)
    return conn


class SqliteAccountRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, account_id: str) -> Account:
        row = self._conn.execute(
            "SELECT id, cents, currency FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"account {account_id} does not exist")
        return Account(id=row[0], balance=Money(row[1], row[2]))

    def save(self, account: Account) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO accounts (id, cents, currency) VALUES (?, ?, ?)",
            (account.id, account.balance.cents, account.balance.currency),
        )


class SqliteLedgerRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, entry: LedgerEntry) -> None:
        amount = entry.amount
        try:
            self._conn.execute(
                "INSERT INTO ledger (id, source_id, target_id, cents, currency, at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (entry.id, entry.source_id, entry.target_id, amount.cents, amount.currency, entry.at),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"ledger entry {entry.id} already exists") from exc

    def for_account(self, account_id: str) -> list[LedgerEntry]:
        rows = self._conn.execute(
            "SELECT id, source_id, target_id, cents, currency, at FROM ledger"
            " WHERE source_id = ? OR target_id = ? ORDER BY at, id",
            (account_id, account_id),
        ).fetchall()
        return [LedgerEntry(r[0], r[1], r[2], Money(r[3], r[4]), r[5]) for r in rows]


class SqliteUnitOfWork:
    """The only place in the codebase that spells BEGIN, COMMIT and ROLLBACK.

    The repositories are built inside ``__enter__``, bound to the transaction it just
    opened. ``BEGIN IMMEDIATE`` takes SQLite's write lock up front instead of at the
    first write, so a lock upgrade cannot fail halfway through. ``commit`` and
    ``rollback`` end the current transaction and start the next, so the block keeps
    its guarantee until ``__exit__`` discards whatever is left. One connection, one thread.
    """

    accounts: AccountRepository
    ledger: LedgerRepository

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> Self:
        self._conn.execute("BEGIN IMMEDIATE")
        self.accounts = SqliteAccountRepository(self._conn)
        self.ledger = SqliteLedgerRepository(self._conn)
        return self

    def __exit__(self, *exc_info: object) -> None:
        del self.accounts, self.ledger  # the repositories live only inside the block
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")  # nothing after a commit; everything after an exception

    def commit(self) -> None:
        self._end_transaction("COMMIT")

    def rollback(self) -> None:
        self._end_transaction("ROLLBACK")

    def _end_transaction(self, statement: str) -> None:
        if not self._conn.in_transaction:
            raise InvalidStateError(f"{statement.lower()}() outside a unit of work")
        self._conn.execute(statement)
        self._conn.execute("BEGIN IMMEDIATE")  # the block stays open; later writes need a commit too


# --8<-- [end:sqlite]


# --8<-- [start:in_memory]
class InMemoryAccountRepository:
    """Writes land in the working copy the unit of work handed over, never in committed state."""

    def __init__(self, accounts: dict[str, Account]) -> None:
        self._accounts = accounts

    def get(self, account_id: str) -> Account:
        try:
            return self._accounts[account_id]
        except KeyError:
            raise NotFoundError(f"account {account_id} does not exist") from None

    def save(self, account: Account) -> None:
        self._accounts[account.id] = account


class InMemoryLedgerRepository:
    def __init__(self, entries: dict[str, LedgerEntry]) -> None:
        self._entries = entries

    def append(self, entry: LedgerEntry) -> None:
        if entry.id in self._entries:
            raise ConflictError(f"ledger entry {entry.id} already exists")
        self._entries[entry.id] = entry

    def for_account(self, account_id: str) -> list[LedgerEntry]:
        mine = (e for e in self._entries.values() if account_id in (e.source_id, e.target_id))
        return sorted(mine, key=lambda e: (e.at, e.id))


class InMemoryUnitOfWork:
    """Change tracking without a database: writes go to a working copy, ``commit`` publishes it.

    ``_lock`` is held from ``__enter__`` to ``__exit__``, so units of work run one at a
    time, the way ``BEGIN IMMEDIATE`` serialises writers in SQLite. It protects the
    committed state (``_accounts``, ``_entries``). Shallow copies are enough because
    the entities are frozen: nothing reachable from the copy can be mutated in place.
    """

    accounts: AccountRepository
    ledger: LedgerRepository

    def __init__(self, accounts: Iterable[Account] = ()) -> None:
        self._accounts: dict[str, Account] = {account.id: account for account in accounts}
        self._entries: dict[str, LedgerEntry] = {}
        self._working_accounts: dict[str, Account] = {}
        self._working_entries: dict[str, LedgerEntry] = {}
        self._active = False
        self._lock = threading.Lock()

    def __enter__(self) -> Self:
        self._lock.acquire()
        self._active = True
        self.accounts = InMemoryAccountRepository(self._working_accounts)
        self.ledger = InMemoryLedgerRepository(self._working_entries)
        self.rollback()  # the working copy starts as the committed state
        return self

    def __exit__(self, *exc_info: object) -> None:
        try:
            self.rollback()  # nothing after a commit; everything after an exception
            del self.accounts, self.ledger
        finally:
            self._active = False
            self._lock.release()

    def commit(self) -> None:
        self._require_open("commit")
        self._accounts = dict(self._working_accounts)  # published under the lock: both or neither
        self._entries = dict(self._working_entries)

    def rollback(self) -> None:
        self._require_open("rollback")
        self._working_accounts.clear()
        self._working_accounts.update(self._accounts)
        self._working_entries.clear()
        self._working_entries.update(self._entries)

    def _require_open(self, operation: str) -> None:
        if not self._active:
            raise InvalidStateError(f"{operation}() outside a unit of work")


# --8<-- [end:in_memory]


# --8<-- [start:service]
class TransferService:
    """The use case. It sees neither sqlite3 nor dicts: open, write, commit once."""

    def __init__(self, uow: UnitOfWork, ids: IdGenerator, clock: Clock) -> None:
        self._uow = uow
        self._ids = ids
        self._clock = clock

    def transfer(self, source_id: str, target_id: str, amount: Money) -> LedgerEntry:
        if amount.cents <= 0:
            raise ValidationError("amount must be positive")
        if source_id == target_id:
            raise ValidationError("source and target must differ")
        with self._uow as uow:
            source = uow.accounts.get(source_id)
            target = uow.accounts.get(target_id)
            uow.accounts.save(source.debit(amount))
            uow.accounts.save(target.credit(amount))
            entry = LedgerEntry(self._ids.next_id(), source_id, target_id, amount, self._clock.now())
            uow.ledger.append(entry)  # a reused id raises here, after both balances were written
            uow.commit()
        return entry

    def balance(self, account_id: str) -> Money:
        with self._uow as uow:  # a read-only unit of work: leaving without commit is fine
            return uow.accounts.get(account_id).balance

    def history(self, account_id: str) -> list[LedgerEntry]:
        with self._uow as uow:
            return uow.ledger.for_account(account_id)


# --8<-- [end:service]


# --8<-- [start:functional]
@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """The pattern reduced to its boundary: a generator-based context manager."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --8<-- [end:functional]


def main() -> None:
    clock = FakeClock(start=1_700_000_000.0)
    opening = [Account("alice", Money.of("100.00")), Account("bob", Money.of("20.00"))]
    conn = connect()
    with SqliteUnitOfWork(conn) as seed:
        for account in opening:
            seed.accounts.save(account)
        seed.commit()

    units: list[tuple[str, UnitOfWork]] = [
        ("sqlite", SqliteUnitOfWork(conn)),
        ("in-memory", InMemoryUnitOfWork(opening)),
    ]
    for label, uow in units:
        print(f"--- transfers over the {label} unit of work ---")
        service = TransferService(uow, SequentialIdGenerator("txn"), clock)
        entry = service.transfer("alice", "bob", Money.of("30.00"))
        print(f"{entry.id}: alice -> bob {entry.amount}; alice={service.balance('alice')} bob={service.balance('bob')}")
        try:
            service.transfer("bob", "alice", Money.of("500.00"))
        except InsufficientFundsError as exc:
            print(f"rejected before any write: {exc}")
        replay = TransferService(uow, SequentialIdGenerator("txn"), clock)  # hands out txn-1 again
        try:
            replay.transfer("alice", "bob", Money.of("10.00"))
        except ConflictError as exc:
            print(f"rolled back after two writes: {exc}")
        print(f"unchanged: alice={service.balance('alice')} bob={service.balance('bob')}")
        print(f"ledger for bob: {[e.id for e in service.history('bob')]}")

    print("--- @contextmanager variant: the boundary alone ---")
    try:
        with transaction(conn) as tx:
            tx.execute("UPDATE accounts SET cents = 0 WHERE id = 'alice'")
            raise RuntimeError("power cut before commit")
    except RuntimeError as exc:
        print(f"block failed: {exc}")
    (cents,) = conn.execute("SELECT cents FROM accounts WHERE id = 'alice'").fetchone()
    print(f"alice still has {Money(cents)}")


if __name__ == "__main__":
    main()
