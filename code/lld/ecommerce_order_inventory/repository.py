"""Repository and Unit of Work: the persistence seam, and how a checkout rolls back."""

from __future__ import annotations

import threading
from typing import Any, Protocol

from common import NotFoundError


# --8<-- [start:repository]
class Repository(Protocol):
    """Collection-like access to one entity type. The only shape services depend on."""

    def add(self, entity: Any) -> None: ...

    def get(self, entity_id: str) -> Any: ...

    def all(self) -> list[Any]: ...


class InMemoryRepository:
    """Dict-backed repository. Swap it for a SQL one without touching a service."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._items: dict[str, Any] = {}
        self._lock = threading.Lock()

    def add(self, entity: Any) -> None:
        with self._lock:
            self._items[entity.id] = entity

    def remove(self, entity_id: str) -> None:
        with self._lock:
            self._items.pop(entity_id, None)

    def get(self, entity_id: str) -> Any:
        with self._lock:
            try:
                return self._items[entity_id]
            except KeyError:
                raise NotFoundError(f"no {self.name} {entity_id}") from None

    def find(self, entity_id: str) -> Any | None:
        with self._lock:
            return self._items.get(entity_id)

    def all(self) -> list[Any]:
        with self._lock:
            return list(self._items.values())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._items)

    def restore(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._items = dict(snapshot)


class UnitOfWork:
    """Group writes across repositories so a failure leaves nothing half-written.

    ``with uow:`` takes a membership snapshot of every repository. Leaving the
    block normally commits; leaving it with an exception restores every snapshot,
    so a checkout that dies after writing the order but before taking payment
    leaves no orphan order behind.

    What it does *not* do is undo field-level edits to entities already in a
    repository -- that needs a Memento, and saying so out loud is the honest
    answer to "is this a real unit of work?". Inventory is deliberately outside
    it: those units live in another service, so giving them back is a
    compensating call, not a rollback. That distinction is the saga in miniature.
    """

    def __init__(self, **repositories: InMemoryRepository) -> None:
        self._repositories = repositories
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        for name, repository in repositories.items():
            setattr(self, name, repository)

    def __enter__(self) -> UnitOfWork:
        self._lock.acquire()
        self._snapshots = {name: repo.snapshot() for name, repo in self._repositories.items()}
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        try:
            if exc_type is not None:
                self.rollback()
        finally:
            self._snapshots = {}
            self._lock.release()
        return False  # never swallow the exception

    def commit(self) -> None:
        self._snapshots = {name: repo.snapshot() for name, repo in self._repositories.items()}

    def rollback(self) -> None:
        for name, snapshot in self._snapshots.items():
            self._repositories[name].restore(snapshot)


# --8<-- [end:repository]
