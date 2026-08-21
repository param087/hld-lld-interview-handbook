"""Cron arithmetic, a recurring task, retries, an early wake-up, pause/resume, shutdown."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

from common import FakeClock, SequentialIdGenerator
from lld.task_scheduler.models import (
    ExecutionRecord,
    OverrunPolicy,
    Priority,
    RetryPolicy,
    TaskStatus,
)
from lld.task_scheduler.schedules import CronSchedule, FixedRate, OneTime
from lld.task_scheduler.services import EventLog, Scheduler

START = datetime(2024, 1, 15, 9, 7, tzinfo=UTC).timestamp()  # a Monday, 09:07 UTC
WAIT = 1.0


def noop() -> None:
    return None


def boom() -> None:
    raise RuntimeError("disk is full")


class Runner:
    """Small helper: advance the fake clock, nudge the timer, wait for the run to land."""

    def __init__(self, clock: FakeClock, scheduler: Scheduler, log: EventLog) -> None:
        self.clock, self.scheduler, self.log = clock, scheduler, log

    def await_run(self, name: str, count: int) -> bool:
        return self.log.wait_for(lambda _: self.log.runs(name) >= count, WAIT)

    def tick(self, seconds: float, name: str, count: int) -> bool:
        self.clock.advance(seconds)
        self.scheduler.wake()  # a real clock would have expired the wait on its own
        return self.await_run(name, count)


def show_schedules(clock: FakeClock) -> None:
    cron = CronSchedule("*/15 * * * *")
    moment, fires = clock.now(), []
    for _ in range(3):
        moment = cron.next_after(moment)
        fires.append(datetime.fromtimestamp(moment, UTC).strftime("%H:%M"))
    print(f"cron '*/15 * * * *' from 09:07 UTC fires at {', '.join(fires)}")

    long_run = ExecutionRecord(attempt=1, started_at=0.0, finished_at=150.0, ok=True)
    skip = FixedRate(60.0, OverrunPolicy.SKIP).next_run(long_run, 150.0)
    catch_up = FixedRate(60.0, OverrunPolicy.CATCH_UP).next_run(long_run, 150.0)
    print(f"fixed rate 60 s after a run of 150 s: skip -> t+{skip:.0f} s, catch-up -> t+{catch_up:.0f} s")


def main() -> None:
    clock = FakeClock(start=START)
    log = EventLog()
    scheduler = Scheduler(workers=2, clock=clock, ids=SequentialIdGenerator("task"), listeners=[log])
    run = Runner(clock, scheduler, log)
    show_schedules(clock)

    scheduler.start()
    ticks = itertools.count(1)
    heartbeat = scheduler.schedule("heartbeat", lambda: next(ticks), FixedRate(30.0))
    run.await_run("heartbeat", 1)
    run.tick(30, "heartbeat", 2)
    run.tick(30, "heartbeat", 3)
    print(f"heartbeat every 30 s: {len(scheduler.task(heartbeat.id).history)} runs in the first minute")

    attempts = itertools.count(1)

    def flaky() -> None:
        if next(attempts) < 3:
            raise ConnectionError("upstream refused the connection")

    retry = RetryPolicy(max_attempts=3, initial_backoff=1.0, multiplier=2.0)
    job = scheduler.schedule("import", flaky, OneTime(clock.now()), retry=retry)
    run.await_run("import", 1)
    run.tick(1, "import", 2)  # first backoff: 1 s
    run.tick(2, "import", 3)  # second backoff: 2 s
    imported = scheduler.task(job.id)
    print(f"import: {imported.status} on attempt {len(imported.history)} after retries at +1 s and +2 s")

    doomed = scheduler.schedule("cleanup", boom, OneTime(clock.now()), retry=RetryPolicy(max_attempts=2))
    run.await_run("cleanup", 1)
    run.tick(1, "cleanup", 2)
    failed = scheduler.task(doomed.id)
    print(f"cleanup: {failed.status} after {len(failed.history)} attempts, last error {failed.history[-1].error!r}")
    print(f"dead letter: {scheduler.dead_letter()}")

    scheduler.schedule("nightly-report", noop, OneTime(clock.now() + 12 * 3600))
    scheduler.schedule("urgent-alert", noop, OneTime(clock.now()), priority=Priority.HIGH)
    woke = run.await_run("urgent-alert", 1)  # no wake() call: the push itself re-armed the timer
    print(f"timer parked 12 h out, then an insertion due now: urgent-alert ran without a nudge = {woke}")

    digest = scheduler.schedule("digest", noop, FixedRate(3600.0, start_delay=3600.0))
    scheduler.pause(digest.id)
    clock.advance(3600)
    scheduler.wake()
    paused = scheduler.task(digest.id)
    print(f"digest paused through its slot: {len(paused.history)} runs, status {paused.status}")
    scheduler.resume(digest.id)
    run.await_run("digest", 1)
    print(f"digest resumed: {len(scheduler.task(digest.id).history)} run, next due in 3600 s")

    scheduler.cancel(digest.id)
    scheduler.cancel(heartbeat.id)
    print(f"cancelled digest and heartbeat: heap still holds {scheduler.pending()} tombstoned entries")

    scheduler.shutdown()
    done = sum(1 for task in scheduler.tasks() if task.status is TaskStatus.SUCCEEDED)
    print(f"shutdown: {len(scheduler.tasks())} tasks registered, {done} succeeded, {len(scheduler.dead_letter())} dead")


if __name__ == "__main__":
    main()
