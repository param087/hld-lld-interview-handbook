"""What a task is, what state it is in, and what one execution left behind.

The schedules live in ``schedules.py``; the queue, the pool and the scheduler
live in ``services.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

from common import InvalidStateError, NotFoundError, ValidationError

if TYPE_CHECKING:
    from lld.task_scheduler.schedules import Schedule


# --8<-- [start:enums]
class TaskStatus(StrEnum):
    SCHEDULED = "scheduled"  # sitting in the heap with a due time
    RUNNING = "running"  # a worker is executing it now
    SUCCEEDED = "succeeded"  # last run passed (a recurring task returns to SCHEDULED)
    RETRYING = "retrying"  # last run failed and a retry is queued
    FAILED = "failed"  # retries exhausted; the id is on the dead-letter list
    PAUSED = "paused"  # stays registered, never dispatched
    CANCELLED = "cancelled"  # terminal, removed from consideration


class Priority(IntEnum):
    """Lower sorts first, so the heap orders by priority without a custom comparator."""

    HIGH = 0
    NORMAL = 1
    LOW = 2


class OverrunPolicy(StrEnum):
    """What a fixed-rate schedule does when a run outlasts its own period."""

    SKIP = "skip"  # jump forward to the next slot in the future: no pile-up
    CATCH_UP = "catch_up"  # run again immediately, once per missed slot


class TaskNotFoundError(NotFoundError):
    """No task with that id is registered."""


class SchedulerStateError(InvalidStateError):
    """The scheduler is not running, or is already shutting down."""


class ScheduleError(ValidationError):
    """A schedule cannot be built or can never fire again."""


# --8<-- [end:enums]


# --8<-- [start:task]
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with a ceiling. Frozen, so one policy is safely shared."""

    max_attempts: int = 3
    initial_backoff: float = 1.0
    multiplier: float = 2.0
    max_backoff: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.initial_backoff <= 0 or self.multiplier < 1:
            raise ValidationError("retry policy must allow at least one attempt with positive backoff")

    def should_retry(self, attempt: int) -> bool:
        return attempt < self.max_attempts

    def backoff(self, attempt: int) -> float:
        """Delay before attempt ``attempt + 1``: 1 s, 2 s, 4 s, ... capped."""
        return min(self.max_backoff, self.initial_backoff * self.multiplier ** (attempt - 1))


DEFAULT_RETRY = RetryPolicy()
NO_RETRY = RetryPolicy(max_attempts=1)


@dataclass(frozen=True, slots=True)
class Task:
    """The Command: the work and how to treat it, with nothing about *when*.

    Separating this from ``ScheduledTask`` is what lets the same callable be
    registered twice under two schedules, and it is what a persistence layer
    stores as the definition rather than as the running state.
    """

    id: str
    name: str
    action: Callable[[], object]
    priority: Priority = Priority.NORMAL
    retry: RetryPolicy = DEFAULT_RETRY
    timeout: float | None = None  # seconds. See the note on cooperative cancellation.


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """One attempt, kept for history and for the schedule's next-run arithmetic."""

    attempt: int
    started_at: float
    finished_at: float
    ok: bool
    error: str | None = None
    timed_out: bool = False

    @property
    def duration(self) -> float:
        return self.finished_at - self.started_at


# --8<-- [end:task]


# --8<-- [start:scheduled]
@dataclass(slots=True)
class ScheduledTask:
    """A task plus when it next runs, what state it is in, and what happened before.

    ``generation`` is the tombstone trick: removing an entry from the middle of a
    heap is O(n), so cancel, pause and reschedule bump this counter instead and a
    popped entry whose generation no longer matches is simply dropped.
    """

    task: Task
    schedule: Schedule
    status: TaskStatus = TaskStatus.SCHEDULED
    next_run_at: float | None = None
    attempt: int = 0
    generation: int = 0
    history: list[ExecutionRecord] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.task.id

    @property
    def name(self) -> str:
        return self.task.name

    @property
    def is_active(self) -> bool:
        return self.status not in (TaskStatus.CANCELLED, TaskStatus.PAUSED, TaskStatus.FAILED)

    def last_run(self) -> ExecutionRecord | None:
        return self.history[-1] if self.history else None

    def summary(self) -> str:
        runs = len(self.history)
        ok = sum(1 for r in self.history if r.ok)
        return f"{self.name}: {self.status} after {runs} run(s), {ok} ok"


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """What a listener receives. Immutable, so it can leave the lock safely."""

    task_id: str
    name: str
    status: TaskStatus
    record: ExecutionRecord | None = None


@dataclass(frozen=True, slots=True, order=True)
class QueueEntry:
    """What the heap orders: due time first, then priority, then arrival.

    ``sequence`` is what makes the ordering total. Without it, two entries with
    the same due time and priority would compare on ``task_id`` - alphabetical
    order masquerading as fairness.
    """

    due_at: float
    priority: Priority
    sequence: int
    task_id: str
    generation: int


# --8<-- [end:scheduled]
