"""Persistence and listener seams, and the scheduler that drives everything."""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Iterable
from functools import partial
from typing import Protocol

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.task_scheduler.models import (
    DEFAULT_RETRY,
    ExecutionRecord,
    Priority,
    QueueEntry,
    RetryPolicy,
    ScheduledTask,
    ScheduleError,
    SchedulerStateError,
    Task,
    TaskEvent,
    TaskNotFoundError,
    TaskStatus,
)
from lld.task_scheduler.queues import (
    DEFAULT_SHUTDOWN_TIMEOUT,
    DEFAULT_WORKERS,
    TaskQueue,
    WorkerPool,
)
from lld.task_scheduler.schedules import Schedule


class TaskStore(Protocol):
    """Persistence seam. Every status change is written through it."""

    def save(self, task: ScheduledTask) -> None: ...

    def delete(self, task_id: str) -> None: ...

    def load_all(self) -> list[ScheduledTask]: ...


class InMemoryTaskStore:
    """The default: keeps the last written state so a test can assert what was persisted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._saved: dict[str, TaskStatus] = {}

    def save(self, task: ScheduledTask) -> None:
        with self._lock:
            self._saved[task.id] = task.status

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._saved.pop(task_id, None)

    def load_all(self) -> list[ScheduledTask]:
        return []

    def statuses(self) -> dict[str, TaskStatus]:
        with self._lock:
            return dict(self._saved)


class TaskListener(Protocol):
    """Observer: told about every status change, always outside the scheduler's lock."""

    def on_task_event(self, event: TaskEvent) -> None: ...


class EventLog:
    """A listener that records events and lets a caller *wait* for one.

    ``wait_for`` is why the tests need no sleeping: a worker thread appends under
    the condition and notifies, and the waiting thread wakes on the notification
    rather than on a timer.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events: list[TaskEvent] = []

    def on_task_event(self, event: TaskEvent) -> None:
        with self._condition:
            self._events.append(event)
            self._condition.notify_all()

    def events(self) -> list[TaskEvent]:
        with self._condition:
            return list(self._events)

    def wait_for(self, predicate: Callable[[list[TaskEvent]], bool], timeout: float = 1.0) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: predicate(self._events), timeout)

    def runs(self, name: str) -> int:
        """Executions recorded for ``name``. A pause or a cancel carries no record."""
        return sum(1 for e in self.events() if e.name == name and e.record is not None)

    def count(self, name: str, status: TaskStatus) -> int:
        return sum(1 for e in self.events() if e.name == name and e.status is status)


# --8<-- [start:scheduler]
class Scheduler:
    """Registry plus timer thread plus worker pool.

    ``_lock`` guards ``_tasks`` and every field of every task in it - status,
    attempt, generation, next run and history. The queue owns its own condition,
    and the two are never held at the same time except in the one direction
    ``_lock`` then ``queue.push``, which is why there is no lock-order cycle.
    """

    def __init__(
        self,
        workers: int = DEFAULT_WORKERS,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        store: TaskStore | None = None,
        listeners: Iterable[TaskListener] = (),
    ) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("task")
        self._store = store or InMemoryTaskStore()
        self._listeners = list(listeners)
        self._lock = threading.Lock()
        self._tasks: dict[str, ScheduledTask] = {}
        self._dead_letter: list[str] = []
        self._sequence = itertools.count()
        self._queue = TaskQueue(self._clock)
        self._pool = WorkerPool(workers, name="scheduler-worker")
        self._timer = threading.Thread(target=self._timer_loop, name="scheduler-timer", daemon=True)
        self._started = False
        self._closed = False

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        if self._started or self._closed:
            raise SchedulerStateError("a scheduler starts once and never restarts after shutdown")
        self._started = True
        self._pool.start()
        self._timer.start()

    def shutdown(self, drain: bool = True, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """Stop timing first, then let the workers finish (or drop) what is in flight.

        Idempotent, so a ``finally`` block and a context manager can both call it.
        """
        if self._closed:
            return
        self._closed = True
        self._queue.close()
        if self._timer.is_alive():
            self._timer.join(timeout)
        self._pool.shutdown(drain=drain, timeout=timeout)

    def wake(self) -> None:
        self._queue.wake()

    # -- registration ------------------------------------------------------------
    def schedule(
        self,
        name: str,
        action: Callable[[], object],
        schedule: Schedule,
        priority: Priority = Priority.NORMAL,
        retry: RetryPolicy = DEFAULT_RETRY,
        timeout: float | None = None,
    ) -> ScheduledTask:
        if self._closed:
            raise SchedulerStateError("the scheduler is shut down and accepts no new tasks")
        due = schedule.first_run(self._clock.now())
        if due is None:
            raise ScheduleError(f"schedule for {name!r} would never fire")
        task = Task(self._ids.next_id(), name, action, priority=priority, retry=retry, timeout=timeout)
        scheduled = ScheduledTask(task=task, schedule=schedule)
        with self._lock:
            self._tasks[task.id] = scheduled
            self._enqueue(scheduled, due, TaskStatus.SCHEDULED)
        return scheduled

    def cancel(self, task_id: str) -> bool:
        """Tombstone the queued entry. A run already in flight finishes on its own."""
        return self._transition(task_id, TaskStatus.CANCELLED, from_active=True)

    def pause(self, task_id: str) -> bool:
        return self._transition(task_id, TaskStatus.PAUSED, from_active=True)

    def resume(self, task_id: str) -> bool:
        with self._lock:
            task = self._require(task_id)
            if task.status is not TaskStatus.PAUSED:
                return False
            due = max(task.next_run_at or self._clock.now(), self._clock.now())
            self._enqueue(task, due, TaskStatus.SCHEDULED)
            return True

    def task(self, task_id: str) -> ScheduledTask:
        with self._lock:
            return self._require(task_id)

    def tasks(self) -> list[ScheduledTask]:
        with self._lock:
            return list(self._tasks.values())

    def dead_letter(self) -> list[str]:
        with self._lock:
            return list(self._dead_letter)

    def pending(self) -> int:
        return len(self._queue)

    # -- the loop ----------------------------------------------------------------
    def _timer_loop(self) -> None:
        while True:
            entry = self._queue.pop_due()
            if entry is None:
                return
            try:
                self._pool.submit(partial(self._execute, entry))
            except SchedulerStateError:
                return

    def _execute(self, entry: QueueEntry) -> None:
        with self._lock:
            task = self._tasks.get(entry.task_id)
            if task is None or entry.generation != task.generation:
                return  # cancelled, paused or superseded: the entry is a tombstone
            if task.status not in (TaskStatus.SCHEDULED, TaskStatus.RETRYING):
                return
            task.status = TaskStatus.RUNNING
            task.attempt += 1
            attempt, action = task.attempt, task.task.action
            limit, retry = task.task.timeout, task.task.retry
        record = self._run_once(attempt, action, limit)
        self._settle(entry.task_id, record, retry)

    def _run_once(self, attempt: int, action: Callable[[], object], limit: float | None) -> ExecutionRecord:
        started = self._clock.now()
        error: str | None = None
        try:
            action()
        except Exception as exc:  # a failing task must never kill its worker thread
            error = f"{type(exc).__name__}: {exc}"
        finished = self._clock.now()
        # Python cannot preempt a thread, so a timeout is *detected*, not enforced.
        # Real cancellation is cooperative: hand the task an Event it checks.
        timed_out = limit is not None and finished - started > limit
        if timed_out and error is None:
            error = f"exceeded its {limit} s budget"
        return ExecutionRecord(attempt, started, finished, ok=error is None, error=error, timed_out=timed_out)

    def _settle(self, task_id: str, record: ExecutionRecord, retry: RetryPolicy) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.history.append(record)
            if task.status is not TaskStatus.RUNNING:
                pass  # cancelled or paused mid-run: keep the record, schedule nothing
            elif record.ok:
                task.attempt = 0
                self._reschedule(task, record)
            elif retry.should_retry(record.attempt):
                self._enqueue(task, record.finished_at + retry.backoff(record.attempt), TaskStatus.RETRYING)
            else:
                task.status = TaskStatus.FAILED
                task.next_run_at = None
                self._dead_letter.append(task.id)
                self._store.save(task)
            event = TaskEvent(task.id, task.name, task.status, record)
        self._notify(event)

    def _reschedule(self, task: ScheduledTask, record: ExecutionRecord) -> None:
        """Caller holds the lock. A recurring task goes straight back into the heap."""
        due = task.schedule.next_run(record, self._clock.now())
        if due is None:
            task.status = TaskStatus.SUCCEEDED
            task.next_run_at = None
            self._store.save(task)
            return
        self._enqueue(task, due, TaskStatus.SCHEDULED)

    def _enqueue(self, task: ScheduledTask, due_at: float, status: TaskStatus) -> None:
        """Caller holds the lock. Bumping the generation retires any older entry."""
        task.generation += 1
        task.status = status
        task.next_run_at = due_at
        self._store.save(task)
        self._queue.push(QueueEntry(due_at, task.task.priority, next(self._sequence), task.id, task.generation))

    def _transition(self, task_id: str, status: TaskStatus, from_active: bool) -> bool:
        with self._lock:
            task = self._require(task_id)
            if from_active and not task.is_active:
                return False
            task.generation += 1  # every queued entry for this task is now stale
            task.status = status
            self._store.save(task)
            event = TaskEvent(task.id, task.name, status)
        self._notify(event)
        return True

    def _require(self, task_id: str) -> ScheduledTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"unknown task {task_id}")
        return task

    def _notify(self, event: TaskEvent) -> None:
        # Outside the lock: a slow listener must never block the scheduler.
        for listener in self._listeners:
            listener.on_task_event(event)


# --8<-- [end:scheduler]
