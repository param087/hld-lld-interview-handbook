import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, FakeClock, NotFoundError, SequentialIdGenerator, ValidationError
from hld.cron_scheduler import JobRun, MisfirePolicy, RunState, Scheduler, Worker


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000.0)


@pytest.fixture
def sched(clock: FakeClock) -> Scheduler:
    return Scheduler(clock, SequentialIdGenerator("run"), lease_seconds=30.0, backoff_base=2.0)


def test_tick_creates_one_run_per_due_slot_and_replays_are_no_ops(sched: Scheduler, clock: FakeClock) -> None:
    sched.add_job("metrics", interval=10)
    created = sched.tick()
    assert [r.job_id for r in created] == ["metrics"]
    assert created[0].scheduled_for == 1_000.0
    assert sched.tick() == []  # the slot is already materialised: unique on (job_id, scheduled_for)
    clock.advance(10)
    second = sched.tick()
    assert [r.scheduled_for for r in second] == [1_010.0]
    assert len(sched.runs_of("metrics")) == 2


def test_priority_decides_which_ready_run_a_worker_gets(sched: Scheduler) -> None:
    sched.add_job("low", interval=100, priority=1)
    sched.add_job("high", interval=100, priority=9)
    sched.add_job("mid", interval=100, priority=5)
    sched.tick()
    order = [sched.claim("w-1"), sched.claim("w-1"), sched.claim("w-1")]
    assert [run.job_id for run in order if run is not None] == ["high", "mid", "low"]
    assert sched.claim("w-1") is None  # nothing ready left


def test_lease_expiry_requeues_the_run_and_the_dead_worker_cannot_complete(
    sched: Scheduler, clock: FakeClock
) -> None:
    sched.add_job("etl", interval=100)
    sched.tick()
    run = sched.claim("w-1")
    assert run is not None and run.state is RunState.RUNNING
    assert sched.reclaim_expired() == []  # the lease is still live
    clock.advance(31)
    reclaimed = sched.reclaim_expired()
    assert [r.run_id for r in reclaimed] == [run.run_id]
    assert run.attempt == 2 and run.state is RunState.PENDING
    with pytest.raises(ConflictError, match="no longer holds the lease"):
        sched.complete(run.run_id, "w-1")
    clock.advance(5)  # 2 ** 2 = 4 s of backoff has elapsed
    again = sched.claim("w-2")
    assert again is not None and again.run_id == run.run_id
    assert sched.complete(again.run_id, "w-2").state is RunState.SUCCEEDED


def test_heartbeat_keeps_a_long_run_out_of_the_reaper(sched: Scheduler, clock: FakeClock) -> None:
    sched.add_job("backup", interval=1_000)
    sched.tick()
    run = sched.claim("w-1")
    assert run is not None
    for _ in range(3):
        clock.advance(20)
        sched.heartbeat(run.run_id, "w-1")
        assert sched.reclaim_expired() == []
    clock.advance(31)
    assert len(sched.reclaim_expired()) == 1
    with pytest.raises(ConflictError):
        sched.heartbeat(run.run_id, "w-1")


def test_failures_back_off_and_end_in_the_dead_letter_list(sched: Scheduler, clock: FakeClock) -> None:
    sched.add_job("flaky", interval=1_000, max_attempts=2)
    sched.tick()

    def boom(run: JobRun) -> None:
        raise RuntimeError("upstream 500")

    worker = Worker("w-1", sched, boom)
    worker.run_once()
    assert sched.counts() == {"pending": 1}
    assert worker.run_once() is None  # still inside the 4 s backoff window
    clock.advance(5)
    worker.run_once()
    dead = sched.dead_letters()
    assert [(r.job_id, r.attempt, r.last_error) for r in dead] == [("flaky", 2, "upstream 500")]
    assert sched.claim("w-2") is None  # a dead run is never handed out again


@pytest.mark.parametrize(
    ("policy", "expected_slots"),
    [
        (MisfirePolicy.SKIP, []),
        (MisfirePolicy.FIRE_ONCE, [1_300.0]),
        (MisfirePolicy.CATCH_UP, [1_200.0, 1_300.0]),
    ],
)
def test_misfire_policy_decides_what_a_missed_window_produces(
    sched: Scheduler, clock: FakeClock, policy: MisfirePolicy, expected_slots: list[float]
) -> None:
    sched.add_job("report", interval=100, misfire=policy, catch_up_limit=2)
    clock.advance(350)  # 3.5 intervals passed with nobody scheduling
    created = sched.tick()
    assert [r.scheduled_for for r in created] == expected_slots
    clock.advance(100)  # the schedule resumes on its own grid whatever the policy
    assert [r.scheduled_for for r in sched.tick()] == [1_400.0]


def test_validation_and_unknown_entities(sched: Scheduler, clock: FakeClock) -> None:
    with pytest.raises(ValidationError):
        sched.add_job("bad", interval=0)
    sched.add_job("ok", interval=10)
    with pytest.raises(ConflictError, match="already exists"):
        sched.add_job("ok", interval=10)
    with pytest.raises(NotFoundError):
        sched.complete("run-999", "w-1")
    with pytest.raises(ValidationError):
        Scheduler(clock, lease_seconds=0)


def test_concurrent_workers_never_execute_the_same_run_twice(sched: Scheduler) -> None:
    for i in range(40):
        sched.add_job(f"job-{i}", interval=1_000, priority=i % 5)
    assert len(sched.tick()) == 40
    seen: list[str] = []
    guard = threading.Lock()

    def record(run: JobRun) -> None:
        with guard:
            seen.append(run.run_id)

    def work(worker_id: int) -> int:
        return Worker(f"w-{worker_id}", sched, record).drain()

    with ThreadPoolExecutor(max_workers=8) as pool:
        drained = list(pool.map(work, range(8)))
    assert sum(drained) == 40
    assert len(seen) == len(set(seen)) == 40  # every run executed exactly once
    assert sched.counts() == {"succeeded": 40}
