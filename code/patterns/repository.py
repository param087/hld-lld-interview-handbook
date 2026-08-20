"""Repository: a collection-like persistence boundary.

The running example is user registration. ``RegistrationService`` talks to *a*
``UserRepository`` as if it were an in-memory collection (``add``, ``get``,
``find_by_email``); ``InMemoryUserRepository`` and ``SqliteUserRepository`` are
interchangeable because they honour the same contract, including which domain
error they raise when a rule is violated. The last section generalises the idea
into a ``Repository[T]`` Protocol with one dict-backed implementation that serves
every entity type in tests.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from common import ConflictError, IdGenerator, NotFoundError, SequentialIdGenerator, ValidationError


# --8<-- [start:contract]
@dataclass(frozen=True, slots=True)
class User:
    """The entity the repository stores. Frozen: a change is a new value passed to ``update``.

    Frozen matters for the in-memory implementation. If ``get`` handed out a mutable
    object, a caller could change it without calling ``update`` and the fake would
    "persist" the change while the SQL implementation would not. Value semantics keep
    both implementations honest in the same way.
    """

    id: str
    email: str
    name: str


@runtime_checkable
class UserRepository(Protocol):
    """The collection-like contract. Domain code sees these six methods and nothing else.

    The contract includes the errors: ``add`` raises ``ConflictError`` when the id or
    the email is already taken; ``update`` and ``remove`` raise ``NotFoundError`` when
    the user does not exist. Every implementation must agree, and the contract tests
    in ``tests/test_repository.py`` run against all of them to prove it.
    """

    def add(self, user: User) -> None: ...

    def get(self, user_id: str) -> User | None: ...

    def find_by_email(self, email: str) -> User | None: ...

    def update(self, user: User) -> None: ...

    def remove(self, user_id: str) -> None: ...

    def count(self) -> int: ...


# --8<-- [end:contract]


# --8<-- [start:in_memory]
class InMemoryUserRepository:
    """A dict-backed implementation: the test fake, and a legitimate small-scale store.

    ``_lock`` protects ``_by_id`` and ``_by_email`` together, so the uniqueness check
    and the insert are one atomic step: exactly what the UNIQUE index gives the SQL
    implementation. Without it two threads could both pass the check and both insert.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, User] = {}
        self._by_email: dict[str, str] = {}  # email -> user id: the "unique index"
        self._lock = threading.Lock()

    def add(self, user: User) -> None:
        with self._lock:
            if user.id in self._by_id:
                raise ConflictError(f"user {user.id} already exists")
            if user.email in self._by_email:
                raise ConflictError(f"email {user.email} is already registered")
            self._by_id[user.id] = user
            self._by_email[user.email] = user.id

    def get(self, user_id: str) -> User | None:
        with self._lock:
            return self._by_id.get(user_id)

    def find_by_email(self, email: str) -> User | None:
        with self._lock:
            user_id = self._by_email.get(email)
            return None if user_id is None else self._by_id[user_id]

    def update(self, user: User) -> None:
        with self._lock:
            current = self._by_id.get(user.id)
            if current is None:
                raise NotFoundError(f"user {user.id} does not exist")
            owner = self._by_email.get(user.email)
            if owner is not None and owner != user.id:
                raise ConflictError(f"email {user.email} is already registered")
            del self._by_email[current.email]
            self._by_email[user.email] = user.id
            self._by_id[user.id] = user

    def remove(self, user_id: str) -> None:
        with self._lock:
            user = self._by_id.pop(user_id, None)
            if user is None:
                raise NotFoundError(f"user {user_id} does not exist")
            del self._by_email[user.email]

    def count(self) -> int:
        with self._lock:
            return len(self._by_id)


# --8<-- [end:in_memory]


# --8<-- [start:sqlite]
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id    TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name  TEXT NOT NULL
)
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open a connection with the schema applied; ``:memory:`` gives every test a fresh database.

    ``autocommit=True`` makes each statement its own transaction, which is what a
    repository used on its own wants. A Unit of Work takes over that decision.
    """
    conn = sqlite3.connect(path, autocommit=True)
    conn.execute(SCHEMA)
    return conn


class SqliteUserRepository:
    """The same contract over a relational table.

    The connection is injected, so whoever owns the transaction decides when the
    writes become visible. Storage exceptions are translated into the domain errors
    the contract promises; callers never import ``sqlite3``. One connection serves
    one thread (``sqlite3``'s default ``check_same_thread``).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, user: User) -> None:
        try:
            self._conn.execute(
                "INSERT INTO users (id, email, name) VALUES (?, ?, ?)",
                (user.id, user.email, user.name),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"user {user.id} or email {user.email} already exists") from exc

    def get(self, user_id: str) -> User | None:
        row = self._conn.execute(
            "SELECT id, email, name FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return _to_user(row)

    def find_by_email(self, email: str) -> User | None:
        row = self._conn.execute(
            "SELECT id, email, name FROM users WHERE email = ?", (email,)
        ).fetchone()
        return _to_user(row)

    def update(self, user: User) -> None:
        try:
            cursor = self._conn.execute(
                "UPDATE users SET email = ?, name = ? WHERE id = ?",
                (user.email, user.name, user.id),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"email {user.email} is already registered") from exc
        if cursor.rowcount == 0:
            raise NotFoundError(f"user {user.id} does not exist")

    def remove(self, user_id: str) -> None:
        cursor = self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        if cursor.rowcount == 0:
            raise NotFoundError(f"user {user_id} does not exist")

    def count(self) -> int:
        (count,) = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return int(count)


def _to_user(row: tuple[str, str, str] | None) -> User | None:
    """The data mapper: one row of the ``users`` table becomes one entity."""
    return None if row is None else User(id=row[0], email=row[1], name=row[2])


# --8<-- [end:sqlite]


# --8<-- [start:service]
class RegistrationService:
    """Domain code: depends on the contract, never on a storage technology.

    The email check here produces a friendly error; the repository's ``add`` is the
    authoritative guard, because two registrations can pass the check at the same
    moment and only the repository (its lock, or its UNIQUE index) sees both.
    """

    def __init__(self, users: UserRepository, ids: IdGenerator) -> None:
        self._users = users
        self._ids = ids

    def register(self, email: str, name: str) -> User:
        email = email.strip().lower()
        if "@" not in email:
            raise ValidationError(f"{email!r} is not an email address")
        if self._users.find_by_email(email) is not None:
            raise ConflictError(f"email {email} is already registered")
        user = User(id=self._ids.next_id(), email=email, name=name)
        self._users.add(user)
        return user

    def rename(self, user_id: str, name: str) -> User:
        user = self._users.get(user_id)
        if user is None:
            raise NotFoundError(f"user {user_id} does not exist")
        renamed = replace(user, name=name)
        self._users.update(renamed)
        return renamed


# --8<-- [end:service]


# --8<-- [start:generic]
@runtime_checkable
class Repository[T](Protocol):
    """The shape every repository shares; ``T`` is the entity type (PEP 695 syntax)."""

    def add(self, item: T) -> None: ...

    def get(self, item_id: str) -> T | None: ...

    def remove(self, item_id: str) -> None: ...

    def __iter__(self) -> Iterator[T]: ...

    def __len__(self) -> int: ...


class InMemoryRepository[T]:
    """One dict-backed implementation for every entity type.

    ``InMemoryRepository[Book](key=lambda book: book.isbn)`` is the fake you write once
    and reuse across an LLD problem's tests. The entity-specific classes above exist
    because ``find_by_email`` and the uniqueness rule belong to users specifically.
    ``_lock`` protects ``_items``.
    """

    def __init__(self, key: Callable[[T], str]) -> None:
        self._key = key
        self._items: dict[str, T] = {}
        self._lock = threading.Lock()

    def add(self, item: T) -> None:
        item_id = self._key(item)
        with self._lock:
            if item_id in self._items:
                raise ConflictError(f"{item_id} already exists")
            self._items[item_id] = item

    def get(self, item_id: str) -> T | None:
        with self._lock:
            return self._items.get(item_id)

    def remove(self, item_id: str) -> None:
        with self._lock:
            if self._items.pop(item_id, None) is None:
                raise NotFoundError(f"{item_id} does not exist")

    def __iter__(self) -> Iterator[T]:
        with self._lock:
            return iter(list(self._items.values()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@dataclass(frozen=True, slots=True)
class Book:
    """A second entity type, identified by ISBN rather than ``id``: the key function copes."""

    isbn: str
    title: str


# --8<-- [end:generic]


def main() -> None:
    repositories: list[tuple[str, UserRepository]] = [
        ("in-memory", InMemoryUserRepository()),
        ("sqlite", SqliteUserRepository(connect())),
    ]
    for label, repo in repositories:
        print(f"--- registration over the {label} repository ---")
        service = RegistrationService(repo, SequentialIdGenerator("user"))
        ada = service.register("Ada@Example.com", "Ada")
        print(f"registered {ada.id} as {ada.email}")
        try:
            service.register("ada@example.com", "Ada again")
        except ConflictError as exc:
            print(f"rejected: {exc}")
        renamed = service.rename(ada.id, "Ada Lovelace")
        print(f"renamed {renamed.id} to {renamed.name!r}; {repo.count()} user(s) stored")
        try:
            service.rename("user-99", "Nobody")
        except NotFoundError as exc:
            print(f"rejected: {exc}")

    print("--- generic dict-backed repository, keyed by a function ---")
    books = InMemoryRepository[Book](key=lambda book: book.isbn)
    books.add(Book("978-0201633610", "Design Patterns"))
    books.add(Book("978-0321127426", "Patterns of Enterprise Application Architecture"))
    print(f"{len(books)} books; isinstance(books, Repository) is {isinstance(books, Repository)}")
    for book in sorted(books, key=lambda book: book.isbn):
        print(f"  {book.isbn}: {book.title}")


if __name__ == "__main__":
    main()
