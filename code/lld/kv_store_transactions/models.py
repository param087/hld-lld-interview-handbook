"""Values, the stored cell, the write-ahead record and the domain errors.

The transaction chain lives in ``transactions.py``; the store, the lock and the
command parser live in ``services.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError

type Value = int | str


# --8<-- [start:errors]
class Operation(StrEnum):
    SET = "set"
    DELETE = "delete"


class NoTransactionError(InvalidStateError):
    """COMMIT or ROLLBACK with nothing open."""


class TransactionConflictError(ConflictError):
    """Under optimistic isolation, a key this transaction read changed underneath it."""


class KeyMissingError(NotFoundError):
    """A strict read (``store[key]``) for a key that is absent or expired."""


class ValueTypeError(ValidationError):
    """INCR or DECR on a value that is not an integer."""


class CommandError(ValidationError):
    """The REPL could not parse or dispatch a line."""


# --8<-- [end:errors]


# --8<-- [start:entry]
@dataclass(frozen=True, slots=True)
class Entry:
    """One committed or staged cell: a value and an optional absolute deadline.

    Frozen because a write replaces the cell rather than mutating it - which is
    what makes a write-set safe to copy into a parent on commit.
    """

    value: Value
    expires_at: float | None = None

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One durable record. A commit appends its whole write-set as a single batch,
    so recovery can never observe half a transaction."""

    operation: Operation
    key: str
    value: Value | None = None
    expires_at: float | None = None

    @classmethod
    def of(cls, key: str, entry: Entry | None) -> LogEntry:
        if entry is None:
            return cls(Operation.DELETE, key)
        return cls(Operation.SET, key, entry.value, entry.expires_at)


# --8<-- [end:entry]
