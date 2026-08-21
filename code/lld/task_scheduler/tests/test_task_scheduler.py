import itertools
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial

import pytest

from common import FakeClock, SequentialIdGenerator, ValidationError
from lld.task_scheduler.models import (
    NO_RETRY,
    ExecutionRecord,
    OverrunPolicy,
    Priority,
    RetryPolicy,
    ScheduleError,
    SchedulerStateError,
    TaskNotFoundError,
    TaskStatus,
)
from lld.task_scheduler.queues import WorkerPool
from lld.task_scheduler.schedules import CronSchedule, FixedDelay, FixedRate, OneTime
from lld.task_scheduler.services import EventLog, InMemoryTaskStore, Scheduler

WAIT = 1.0
NEVER = 0.05  # bounded wait used to prove that something does *not* happen

type Factory = Callable[..., tuple[Scheduler, EventLog]]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_700_000_000)


@pytest.fixture
def make_scheduler(clock: FakeClock) -> Iterator[Factory]:
    built: list[Scheduler] = []

    def factory(workers: int = 2, **kwargs: object) -> tuple[Scheduler, EventLog]:
        log = EventLog()
        scheduler = Scheduler(
            workers=workers,
            clock=clock,
            ids=SequentialIdGenerator("t"),
            listeners=[log],
            **kwargs,  # type: ignore[arg-type]
        )
        built.append(scheduler)
        return scheduler, log

    yield factory
    for scheduler in built:
        scheduler.shutdown(timeout=0.5)


def record(started: float, finished: float) -> ExecutionRecord:
    return ExecutionRecord(attempt=1, started_at=started, finished_at=finished, ok=True)


def test_one_time_task_runs_once_and_retires(clock: FakeClock, make_scheduler: Factory) -> None:
    scheduler, log = make_scheduler()
    calls: list[int] = []
    task = scheduler.schedule("backup", lambda: calls.append(1), OneTime(clock.now()))
    scheduler.start()
    assert log.wait_for(lambda _: log.runs("backup") >= 1, WAIT)
    assert calls == [1]
    stored = scheduler.task(task.id)
    assert stored.status is TaskStatus.SUCCEEDED and stored.next_run_at is None
    assert scheduler.pending() == 0


@pytest.mark.parametrize(
    ("policy", "expected"),
    [(OverrunPolicy.SKIP, 180.0), (OverrunPolicy.CATCH_UP, 60.0)],
)
def test_fixed_rate_overrun_policy(policy: OverrunPolicy, expected: float) -> None:
    """A run that started at 0 and finished at 150 has blown through two 60 s slots."""
    schedule = FixedRate(60.0, on_overrun=policy)
    assert schedule.next_run(record(0.0, 150.0), now=150.0) == expected
    assert schedule.next_run(record(0.0, 10.0), now=10.0) == 60.0  # a normal run is unaffected


def test_fixed_delay_measures_from_the_finish() -> None:
    assert FixedDelay(30.0).next_run(record(0.0, 150.0), now=150.0) == 180.0


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("*/15 * * * *", "2024-01-15 09:15"),
        ("0 3 * * *", "2024-01-16 03:00"),
        ("0 3 * * 1-5", "2024-01-16 03:00"),
        ("30 9 * * *", "2024-01-15 09:30"),
        ("0 0 1 * *", "2024-02-01 00:00"),
    ],
)
def test_cron_finds_the_next_fire_time(expression: str, expected: str) -> None:
    start = datetime(2024, 1, 15, 9, 7, tzinfo=UTC).timestamp()  # a Monday
    fires_at = CronSchedule(expression).next_after(start)
    assert datetime.fromtimestamp(fires_at, UTC).strftime("%Y-%m-%d %H:%M") == expected


@pytest.mark.parametrize("expression", ["* * * *", "61 * * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *"])
def test_cron_rejects_impossible_expressions(expression: str) -> None:
    with pytest.raises(ScheduleError):
        CronSchedule(expression)


# --8<-- [start:wakeup]
def test_an_earlier_insertion_wakes_the_parked_timer(clock: FakeClock, make_scheduler: Factory) -> None:
    """The reason the queue is a Condition and not a sleep.

    The timer parks with a twelve-hour timeout. Pushing a task due *now* has to
    interrupt that wait, or the urgent task waits half a day. Nothing here
    advances the clock and nothing calls `wake()`: the notify inside `push` is
    what re-arms the timer.
    """
    scheduler, log = make_scheduler(workers=1)
    scheduler.schedule("nightly", lambda: None, OneTime(clock.now() + 12 * 3600))
    scheduler.start()
    assert not log.wait_for(lambda _: log.runs("nightly") >= 1, NEVER)

    scheduler.schedule("urgent", lambda: None, OneTime(clock.now()), priority=Priority.HIGH)
    assert log.wait_for(lambda _: log.runs("urgent") >= 1, WAIT)
    assert log.runs("nightly") == 0  # the twelve-hour task is still waiting its turn


# --8<-- [end:wakeup]


def test_retry_backoff_then_success(clock: FakeClock, make_scheduler: Factory) -> None:
    scheduler, log = make_scheduler()
    attempts = itertools.count(1)

    def flaky() -> None:
        if next(attempts) < 3:
            raise ConnectionError("upstream refused")

    policy = RetryPolicy(max_attempts=3, initial_backoff=1.0, multiplier=2.0)
    task = scheduler.schedule("import", flaky, OneTime(clock.now()), retry=policy)
    scheduler.start()
    assert log.wait_for(lambda _: log.runs("import") >= 1, WAIT)
    assert scheduler.task(task.id).status is TaskStatus.RETRYING

    for backoff, total in ((1, 2), (2, 3)):
        clock.advance(backoff)
        scheduler.wake()
        assert log.wait_for(lambda _, n=total: log.runs("import") >= n, WAIT)

    stored = scheduler.task(task.id)
    assert stored.status is TaskStatus.SUCCEEDED and len(stored.history) == 3
    assert [r.ok for r in stored.history] == [False, False, True]
    assert stored.attempt == 0  # the counter resets, so a recurring task retries afresh


def test_exhausted_retries_land_in_the_dead_letter(clock: FakeClock, make_scheduler: Factory) -> None:
    store = InMemoryTaskStore()
    scheduler, log = make_scheduler(store=store)

    def boom() -> None:
        raise RuntimeError("disk is full")

    task = scheduler.schedule("cleanup", boom, OneTime(clock.now()), retry=RetryPolicy(max_attempts=2))
    scheduler.start()
    assert log.wait_for(lambda _: log.runs("cleanup") >= 1, WAIT)
    clock.advance(1)
    scheduler.wake()
    assert log.wait_for(lambda _: log.runs("cleanup") >= 2, WAIT)
    assert scheduler.task(task.id).status is TaskStatus.FAILED
    assert scheduler.dead_letter() == [task.id]
    assert store.statuses()[task.id] is TaskStatus.FAILED  # the store saw every transition


def test_cancel_tombstones_a_queued_entry(clock: FakeClock, make_scheduler: Factory) -> None:
    scheduler, log = make_scheduler()
    task = scheduler.schedule("report", lambda: None, OneTime(clock.now() + 10))
    scheduler.start()
    assert scheduler.cancel(task.id) is True
    assert scheduler.cancel(task.id) is False  # already terminal
    clock.advance(10)
    scheduler.wake()
    assert not log.wait_for(lambda _: log.runs("report") >= 1, NEVER)
    assert scheduler.task(task.id).status is TaskStatus.CANCELLED
    with pytest.raises(TaskNotFoundError):
        scheduler.cancel("nope")


def test_pause_holds_a_slot_and_resume_runs_it(clock: FakeClock, make_scheduler: Factory) -> None:
    scheduler, log = make_scheduler()
    task = scheduler.schedule("digest", lambda: None, FixedRate(3600.0, start_delay=3600.0))
    scheduler.start()
    assert scheduler.pause(task.id) is True
    clock.advance(3600)
    scheduler.wake()
    assert not log.wait_for(lambda _: log.runs("digest") >= 1, NEVER)
    assert scheduler.resume(task.id) is True
    assert log.wait_for(lambda _: log.runs("digest") >= 1, WAIT)
    assert scheduler.resume(task.id) is False  # not paused any more


def test_priority_breaks_a_tie_between_tasks_due_together(clock: FakeClock, make_scheduler: Factory) -> None:
    scheduler, log = make_scheduler(workers=1)
    for name, priority in (("low", Priority.LOW), ("high", Priority.HIGH), ("normal", Priority.NORMAL)):
        scheduler.schedule(name, lambda: None, OneTime(clock.now()), priority=priority)
    scheduler.start()
    assert log.wait_for(lambda events: sum(1 for e in events if e.record) >= 3, WAIT)
    order = [event.name for event in log.events() if event.record is not None]
    assert order == ["high", "normal", "low"]


# --8<-- [start:concurrency]
def test_fifty_tasks_due_at_once_each_run_exactly_once(clock: FakeClock, make_scheduler: Factory) -> None:
    """Four workers, fifty tasks, one instant: no task is skipped and none runs twice.

    The invariant is what a broken heap or an unguarded status transition breaks:
    a task popped twice would appear twice in `seen`, and one dropped by a lost
    notify would never appear at all.
    """
    scheduler, log = make_scheduler(workers=4)
    seen: list[int] = []
    for i in range(50):
        scheduler.schedule(f"job-{i}", partial(seen.append, i), OneTime(clock.now()))
    scheduler.start()
    assert log.wait_for(lambda events: sum(1 for e in events if e.record) >= 50, 2.0)
    assert sorted(seen) == list(range(50))
    assert scheduler.pending() == 0
    assert all(task.status is TaskStatus.SUCCEEDED for task in scheduler.tasks())


# --8<-- [end:concurrency]


def test_a_task_over_its_budget_is_recorded_as_timed_out(clock: FakeClock, make_scheduler: Factory) -> None:
    scheduler, log = make_scheduler()
    task = scheduler.schedule(
        "slow", lambda: clock.advance(5), OneTime(clock.now()), retry=NO_RETRY, timeout=1.0
    )
    scheduler.start()
    assert log.wait_for(lambda _: log.runs("slow") >= 1, WAIT)
    last = scheduler.task(task.id).history[-1]
    assert last.timed_out and not last.ok and last.duration == 5.0
    assert scheduler.task(task.id).status is TaskStatus.FAILED


def test_shutdown_is_final_and_idempotent(clock: FakeClock, make_scheduler: Factory) -> None:
    scheduler, log = make_scheduler()
    calls: list[int] = []
    scheduler.schedule("first", lambda: calls.append(1), OneTime(clock.now()))
    scheduler.start()
    assert log.wait_for(lambda _: log.runs("first") >= 1, WAIT)
    scheduler.shutdown()
    scheduler.shutdown()  # a second call must be harmless
    with pytest.raises(SchedulerStateError):
        scheduler.schedule("late", lambda: None, OneTime(clock.now()))
    with pytest.raises(SchedulerStateError):
        scheduler.start()
    assert calls == [1]


def test_worker_pool_drains_what_is_queued_then_refuses_new_work() -> None:
    done: list[int] = []
    pool = WorkerPool(size=2, name="test-worker")
    pool.start()
    for i in range(10):
        pool.submit(partial(done.append, i))
    pool.shutdown(drain=True, timeout=1.0)
    assert sorted(done) == list(range(10))
    with pytest.raises(SchedulerStateError):
        pool.submit(lambda: None)
    with pytest.raises(ValidationError):
        WorkerPool(size=0)
