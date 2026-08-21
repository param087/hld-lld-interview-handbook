"""Levels, the log record, the sink abstraction and the domain errors.

Nothing here writes anything: destinations live in ``handlers.py``, the
representation in ``formatters.py``, and the hierarchy in ``services.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Protocol

from common import ConflictError, HandbookError, InvalidStateError, ValidationError


# --8<-- [start:levels]
class LogLevel(IntEnum):
    """Ordered on purpose: threshold filtering is one integer comparison.

    ``NOTSET`` means "ask my parent" and is what makes the hierarchy work:
    a logger only overrides the inherited threshold when it sets a real level.
    """

    NOTSET = 0
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50

    def __str__(self) -> str:
        return self.name


class OverflowPolicy(StrEnum):
    """What an ``AsyncHandler`` does when its bounded queue is full."""

    BLOCK = "block"  # backpressure: the caller waits (never lose a record)
    DROP_NEWEST = "drop_newest"  # shed load: the caller never waits
    DROP_OLDEST = "drop_oldest"  # keep the freshest window of history


class WorkerState(StrEnum):
    IDLE = "idle"  # constructed, worker thread not started
    RUNNING = "running"  # worker consuming the queue
    DRAINING = "draining"  # close() called, finishing the backlog
    STOPPED = "stopped"  # worker joined, sink closed


# --8<-- [end:levels]


# --8<-- [start:errors]
class LoggingConfigError(ValidationError):
    """A logger name, level or handler wiring that cannot be honoured."""


class QueueOverflowError(ConflictError):
    """A bounded queue rejected a record under the BLOCK policy timeout."""


class LoggerShutdownError(InvalidStateError):
    """The manager has been shut down; handlers no longer accept records."""


class HandlerFailure(HandbookError):
    """Raised by a handler's sink. Captured by the manager, never re-raised at the call site."""


# --8<-- [end:errors]


# --8<-- [start:record]
@dataclass(frozen=True, slots=True)
class LogRecord:
    """One immutable logging event.

    It is created once by ``LogManager.make_record`` and then shared by every
    handler in the chain, so it must never be mutated: two handlers formatting
    the same record concurrently is the common case, not the exception.
    """

    id: str
    logger_name: str
    level: LogLevel
    message: str
    created: float  # epoch seconds from the injected Clock
    thread_name: str
    context: dict[str, str] = field(default_factory=dict)

    def is_at_least(self, level: LogLevel) -> bool:
        return self.level >= level


# --8<-- [end:record]


# --8<-- [start:sinks]
class Stream(Protocol):
    """The two calls a file-like destination has to support."""

    def write(self, text: str) -> int: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


class FileSystem(Protocol):
    """Injected so rotation is testable without touching a real disk."""

    def open_append(self, path: str) -> Stream: ...
    def size(self, path: str) -> int: ...
    def exists(self, path: str) -> bool: ...
    def rename(self, src: str, dst: str) -> None: ...
    def remove(self, path: str) -> None: ...


class Transport(Protocol):
    """Where ``RemoteHandler`` ships a batch of formatted lines."""

    def send(self, lines: list[str]) -> None: ...


# --8<-- [end:sinks]


class Filter(Protocol):
    """Predicate applied to a record. Returning ``False`` drops it silently."""

    def allows(self, record: LogRecord) -> bool: ...
