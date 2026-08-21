"""A distributed job scheduler: a due-time heap, idempotent run records and leased execution.

What the module demonstrates, in the order an interviewer asks about it:

* ``Scheduler.tick`` pops every job whose ``next_run_at`` has passed from a min-heap and
  materialises a ``JobRun`` per due slot. The run key is ``(job_id, scheduled_for)``, so two
  schedulers racing during a failover create the same row, not two: the unique index turns a
  double-schedule into a no-op.
* A misfire (the scheduler was down, or a run was slow) is a policy, not an accident:
  ``SKIP`` forgets the missed window, ``FIRE_ONCE`` runs the most recent slot, ``CATCH_UP``
  backfills up to a bounded number of them.
* Execution is **leased**, not assigned. ``claim`` hands a run to one worker for
  ``lease_seconds``; only the lease owner may ``complete`` it, and ``reclaim_expired`` requeues
  the runs of workers that died. Delivery is therefore at-least-once and handlers must be
  idempotent, which is what the run record's identity is for.
* Retries go to a delayed heap with exponential backoff; a run that exhausts ``max_attempts``
  lands in the dead-letter list instead of looping forever.
"""

from __future__ import annotations

import heapq
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from common import (
    Clock,
    ConflictError,
    IdGenerator,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)


# --8<-- [start:models]
class RunState(StrEnum):
    PENDING = "pending"  # waiting for a worker (or for its backoff to elapse)
    RUNNING = "running"  # leased to a worker
    SUCCEEDED = "succeeded"
    DEAD = "dead"  # attempts exhausted: parked in the dead-letter list


class MisfirePolicy(StrEnum):
    """What to do about slots that passed while nobody was scheduling."""

    SKIP = "skip"  # forget them, resume at the next future slot
    FIRE_ONCE = "fire_once"  # run the most recent missed slot once, then resume
    CATCH_UP = "catch_up"  # backfill the missed slots, up to catch_up_limit


@dataclass(slots=True)
class Job:
    job_id: str
    interval: float  # seconds between slots; a cron expression compiles down to the same thing
    next_run_at: float
    priority: int = 0  # higher wins when several runs are ready at once
    max_attempts: int = 3
    misfire: MisfirePolicy = MisfirePolicy.FIRE_ONCE
    catch_up_limit: int = 5
    enabled: bool = True


@dataclass(slots=True)
class JobRun:
    run_id: str
    job_id: str
    scheduled_for: float  # the slot this run belongs to: half of its identity
    priority: int
    state: RunState = RunState.PENDING
    attempt: int = 1
    available_at: float = 0.0  # backoff deadline for a retry
    lease_owner: str | None = None
    lease_expires_at: float = 0.0
    last_error: str = ""


# --8<-- [end:models]


# --8<-- [start:scheduler]
class Scheduler:
    """Schedules jobs and leases their runs. ``_lock`` guards every collection below.

    In production ``_due`` is an index scan (``WHERE next_run_at <= now ORDER BY next_run_at``)
    or a time wheel, ``_runs`` is a table with a unique index on ``(job_id, scheduled_for)``,
    and ``claim`` is ``UPDATE run SET owner = ?, lease_expires_at = ? WHERE id = ? AND
    (owner IS NULL OR lease_expires_at < now)`` with a row-count check.
    """

    def __init__(
        self,
        clock: Clock,
        ids: IdGenerator | None = None,
        lease_seconds: float = 30.0,
        backoff_base: float = 2.0,
    ) -> None:
        if lease_seconds <= 0 or backoff_base <= 1:
            raise ValidationError("lease_seconds must be positive and backoff_base above 1")
        self._clock = clock
        self._ids = ids or SequentialIdGenerator("run")
        self._lease = lease_seconds
        self._backoff = backoff_base
        self._jobs: dict[str, Job] = {}
        self._due: list[tuple[float, str]] = []  # (next_run_at, job_id) min-heap
        self._runs: dict[str, JobRun] = {}
        self._keys: set[tuple[str, float]] = set()  # (job_id, scheduled_for): the unique index
        self._ready: list[tuple[int, float, str]] = []  # (-priority, scheduled_for, run_id)
        self._delayed: list[tuple[float, str]] = []  # (available_at, run_id) for backed-off runs
        self._dead: list[JobRun] = []
        self._lock = threading.Lock()

    # -- registration ------------------------------------------------------------------------
    def add_job(self, job_id: str, interval: float, first_run_at: float | None = None, **kwargs: object) -> Job:
        if interval <= 0:
            raise ValidationError("interval must be positive")
        with self._lock:
            if job_id in self._jobs:
                raise ConflictError(f"job {job_id} already exists")
            job = Job(job_id, interval, first_run_at if first_run_at is not None else self._clock.now(), **kwargs)  # type: ignore[arg-type]
            self._jobs[job_id] = job
            heapq.heappush(self._due, (job.next_run_at, job_id))
            return job

    # -- the scheduling tick ------------------------------------------------------------------
    def tick(self) -> list[JobRun]:
        """Materialise a run for every due slot. Safe to call from two schedulers at once."""
        now = self._clock.now()
        created: list[JobRun] = []
        with self._lock:
            while self._due and self._due[0][0] <= now:
                _, job_id = heapq.heappop(self._due)
                job = self._jobs[job_id]
                if not job.enabled:
                    continue
                for slot in self._slots(job, now):
                    run = self._materialise(job, slot)
                    if run is not None:
                        created.append(run)
                heapq.heappush(self._due, (job.next_run_at, job_id))
            self._promote(now)
        return created

    @staticmethod
    def _slots(job: Job, now: float) -> list[float]:
        """Every due slot, filtered by the misfire policy. Also advances ``next_run_at``."""
        slots: list[float] = []
        cursor = job.next_run_at
        while cursor <= now:
            slots.append(cursor)
            cursor += job.interval
        job.next_run_at = cursor
        if len(slots) <= 1:  # on time: nothing was missed
            return slots
        if job.misfire is MisfirePolicy.SKIP:
            return []
        if job.misfire is MisfirePolicy.FIRE_ONCE:
            return slots[-1:]
        return slots[-job.catch_up_limit :]

    def _materialise(self, job: Job, slot: float) -> JobRun | None:
        if (job.job_id, slot) in self._keys:
            return None  # another scheduler already created this run: the unique index wins
        run = JobRun(self._ids.next_id(), job.job_id, slot, job.priority, available_at=slot)
        self._keys.add((job.job_id, slot))
        self._runs[run.run_id] = run
        heapq.heappush(self._ready, (-run.priority, run.scheduled_for, run.run_id))
        return run

    def _promote(self, now: float) -> None:
        """Move backed-off runs whose delay has elapsed into the ready heap."""
        while self._delayed and self._delayed[0][0] <= now:
            _, run_id = heapq.heappop(self._delayed)
            run = self._runs[run_id]
            heapq.heappush(self._ready, (-run.priority, run.scheduled_for, run_id))

    # -- leased execution ----------------------------------------------------------------------
    def claim(self, worker_id: str) -> JobRun | None:
        """Lease the highest-priority ready run to ``worker_id``, or return None."""
        now = self._clock.now()
        with self._lock:
            self._promote(now)
            while self._ready:
                _, _, run_id = heapq.heappop(self._ready)
                run = self._runs[run_id]
                if run.state is not RunState.PENDING or run.available_at > now:
                    continue
                run.state = RunState.RUNNING
                run.lease_owner = worker_id
                run.lease_expires_at = now + self._lease
                return run
            return None

    def heartbeat(self, run_id: str, worker_id: str) -> None:
        """Extend the lease of a long-running job so the reclaimer leaves it alone."""
        with self._lock:
            run = self._own(run_id, worker_id)
            run.lease_expires_at = self._clock.now() + self._lease

    def complete(self, run_id: str, worker_id: str) -> JobRun:
        """Only the live lease owner may record success: this is what stops a double write."""
        with self._lock:
            run = self._own(run_id, worker_id)
            run.state = RunState.SUCCEEDED
            run.lease_owner = None
            return run

    def fail(self, run_id: str, worker_id: str, error: str) -> JobRun:
        with self._lock:
            run = self._own(run_id, worker_id)
            return self._requeue(run, error)

    def reclaim_expired(self) -> list[JobRun]:
        """The reaper: a worker that stopped heart-beating loses its runs to somebody else."""
        now = self._clock.now()
        with self._lock:
            expired = [
                run
                for run in self._runs.values()
                if run.state is RunState.RUNNING and run.lease_expires_at <= now
            ]
            return [self._requeue(run, "lease expired") for run in expired]

    def _requeue(self, run: JobRun, error: str) -> JobRun:
        job = self._jobs[run.job_id]
        run.last_error = error
        run.lease_owner = None
        if run.attempt >= job.max_attempts:
            run.state = RunState.DEAD
            self._dead.append(run)
            return run
        run.attempt += 1
        run.state = RunState.PENDING
        run.available_at = self._clock.now() + self._backoff**run.attempt
        heapq.heappush(self._delayed, (run.available_at, run.run_id))
        return run

    def _own(self, run_id: str, worker_id: str) -> JobRun:
        run = self._runs.get(run_id)
        if run is None:
            raise NotFoundError(f"unknown run {run_id}")
        if run.lease_owner != worker_id or run.lease_expires_at <= self._clock.now():
            raise ConflictError(f"{worker_id} no longer holds the lease on {run_id}")
        return run

    # -- read path ------------------------------------------------------------------------------
    def dead_letters(self) -> list[JobRun]:
        with self._lock:
            return list(self._dead)

    def runs_of(self, job_id: str) -> list[JobRun]:
        with self._lock:
            return sorted(
                (r for r in self._runs.values() if r.job_id == job_id), key=lambda r: r.scheduled_for
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            tally: dict[str, int] = {}
            for run in self._runs.values():
                tally[run.state.value] = tally.get(run.state.value, 0) + 1
            return tally


# --8<-- [end:scheduler]


# --8<-- [start:worker]
Handler = Callable[[JobRun], None]


@dataclass(slots=True)
class Worker:
    """One executor process: claim, run, then complete or fail under the lease it holds.

    The handler is called outside the scheduler's lock, so a slow job never blocks scheduling.
    It must be idempotent: at-least-once delivery means it can see the same
    ``(job_id, scheduled_for)`` twice after a lease expiry.
    """

    worker_id: str
    scheduler: Scheduler
    handler: Handler
    executed: list[str] = field(default_factory=list)

    def run_once(self) -> JobRun | None:
        run = self.scheduler.claim(self.worker_id)
        if run is None:
            return None
        try:
            self.handler(run)
        except Exception as exc:  # noqa: BLE001 - a failing job is data, not a crash
            return self.scheduler.fail(run.run_id, self.worker_id, str(exc))
        self.executed.append(run.run_id)
        return self.scheduler.complete(run.run_id, self.worker_id)

    def drain(self, limit: int = 100) -> int:
        done = 0
        while done < limit and self.run_once() is not None:
            done += 1
        return done


# --8<-- [end:worker]


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=1_000.0)
    sched = Scheduler(clock, SequentialIdGenerator("run"), lease_seconds=30.0)
    sched.add_job("metrics", interval=10, priority=1)
    sched.add_job("billing", interval=60, priority=5)
    sched.add_job("report", interval=100, priority=9, misfire=MisfirePolicy.CATCH_UP, catch_up_limit=3)
    created = sched.tick()
    print(f"tick at t=0: 3 jobs due            -> runs {[r.job_id for r in created]}")
    print(f"the same tick replayed             -> {len(sched.tick())} new runs (unique on job_id + slot)")

    log: list[str] = []
    worker = Worker("w-1", sched, lambda run: log.append(run.job_id))
    worker.run_once()
    worker.run_once()
    print(f"w-1 claims twice                   -> executed {log} (priority 9 before 5)")

    stuck = sched.claim("w-2")
    if stuck is None:
        raise RuntimeError("the demo expects a ready run here")
    print(f"w-2 claims {stuck.job_id} then dies        -> lease held until t={stuck.lease_expires_at:.0f}")
    clock.advance(31)
    reclaimed = sched.reclaim_expired()
    print(f"31 s later, the reaper runs        -> requeued {[r.job_id for r in reclaimed]}, attempt {reclaimed[0].attempt}")
    try:
        sched.complete(stuck.run_id, "w-2")
    except ConflictError as exc:
        print(f"w-2 wakes up and completes late    -> rejected: {exc}")

    clock.advance(5)  # past the 2**2 = 4 s backoff
    survivor = Worker("w-3", sched, lambda run: log.append(f"{run.job_id}#{run.attempt}"))
    survivor.run_once()
    print(f"w-3 picks up the requeued run      -> executed {log[-1]}, states {sched.counts()}")

    def always_fails(run: JobRun) -> None:
        raise RuntimeError("upstream 500")

    sched.add_job("flaky", interval=1_000, priority=7, max_attempts=2)
    sched.tick()
    breaker = Worker("w-4", sched, always_fails)
    breaker.run_once()
    clock.advance(9)  # 2**2 backoff elapsed
    breaker.run_once()
    dead = sched.dead_letters()
    print(f"flaky fails twice (max_attempts 2) -> dead letters {[(r.job_id, r.last_error) for r in dead]}")

    clock.advance(350)  # the scheduler was down for 3.5 report intervals
    misfired = sched.tick()
    slots = [r.scheduled_for for r in misfired if r.job_id == "report"]
    print(f"scheduler down 350 s, catch_up 3   -> report backfilled {len(slots)} slots at {[f'{s:.0f}' for s in slots]}")
    print(f"metrics uses fire_once             -> {len([r for r in misfired if r.job_id == 'metrics'])} run, not 35")


if __name__ == "__main__":
    main()
