from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, FakeClock, NotFoundError, ValidationError
from hld.judge_runner import (
    CaseResult,
    Judge,
    Limits,
    ProblemCase,
    Program,
    SubmissionQueue,
    TokenChecker,
    Verdict,
)

CASES = [
    ProblemCase("t1", "2 3", "5", is_sample=True),
    ProblemCase("t2", "10 20", "30"),
    ProblemCase("t3", "big", "1000000"),
]
LIMITS = Limits(time_ms=2_000, memory_kb=256 * 1024, output_kb=1)


def test_a_correct_submission_passes_every_case_and_reports_its_peak_cost() -> None:
    program = Program({"2 3": "5\n", "10 20": " 30 ", "big": "1000000"}, time_ms=120, memory_kb=8_000)
    result = Judge().judge(program, CASES, LIMITS)
    assert result.verdict is Verdict.ACCEPTED
    assert (result.cases_passed, result.total_cases, result.failed_case_id) == (3, 3, None)
    assert (result.max_time_ms, result.max_memory_kb) == (120, 8_000)


def test_judging_stops_at_the_first_failing_case_and_names_it() -> None:
    program = Program({"2 3": "5", "10 20": "31"})
    seen: list[CaseResult] = []
    result = Judge().judge(program, CASES, LIMITS, on_case=seen.append)
    assert result.verdict is Verdict.WRONG_ANSWER
    assert (result.cases_passed, result.failed_case_id) == (1, "t2")
    assert [r.case_id for r in seen] == ["t1", "t2"]  # t3 never ran
    assert seen[-1].verdict is Verdict.WRONG_ANSWER


@pytest.mark.parametrize(
    ("program", "expected"),
    [
        (Program({"2 3": "5"}, time_ms=4_000), Verdict.TIME_LIMIT_EXCEEDED),
        (Program({"2 3": "5"}, memory_kb=512 * 1024), Verdict.MEMORY_LIMIT_EXCEEDED),
        (Program({"2 3": "5"}, crashes_on=frozenset({"2 3"})), Verdict.RUNTIME_ERROR),
        (Program(compile_error="line 7: expected ';'"), Verdict.COMPILE_ERROR),
        (Program({"2 3": "x" * 4096}), Verdict.OUTPUT_LIMIT_EXCEEDED),
        (Program({"2 3": "6"}), Verdict.WRONG_ANSWER),
    ],
)
def test_the_verdict_taxonomy(program: Program, expected: Verdict) -> None:
    assert Judge().judge(program, CASES, LIMITS).verdict is expected


def test_a_memory_kill_is_not_reported_as_a_runtime_error() -> None:
    """The out-of-memory killer also exits non-zero, so precedence decides the verdict."""
    hungry = Program({"2 3": "5"}, memory_kb=512 * 1024, crashes_on=frozenset({"2 3"}))
    assert Judge().judge(hungry, CASES, LIMITS).verdict is Verdict.MEMORY_LIMIT_EXCEEDED


def test_the_checker_is_whitespace_insensitive_but_not_order_insensitive() -> None:
    checker = TokenChecker()
    assert checker.matches("5", "5\n")
    assert checker.matches("1 2 3", " 1\n2\t3 \n")
    assert not checker.matches("1 2 3", "3 2 1")


def test_the_same_submission_judges_identically_every_time() -> None:
    program = Program({"2 3": "5", "10 20": "30", "big": "999"})
    judge = Judge()
    runs = [judge.judge(program, CASES, LIMITS) for _ in range(5)]
    assert all(run == runs[0] for run in runs)
    assert runs[0].verdict is Verdict.WRONG_ANSWER


def test_an_expired_lease_returns_the_submission_to_the_queue() -> None:
    clock = FakeClock(start=1_000.0)
    queue = SubmissionQueue(clock=clock, lease_seconds=30.0, max_attempts=2)
    queue.submit("s1")
    lease = queue.claim()
    assert lease is not None and lease.submission_id == "s1"
    assert queue.depth() == (0, 1)
    clock.advance(10)
    assert queue.reclaim_expired() == []  # the lease is still valid
    clock.advance(21)
    assert queue.reclaim_expired() == ["s1"]
    assert queue.depth() == (1, 0)


def test_a_submission_that_kills_every_worker_becomes_a_dead_letter() -> None:
    clock = FakeClock(start=0.0)
    queue = SubmissionQueue(clock=clock, lease_seconds=10.0, max_attempts=2)
    queue.submit("poison")
    for _ in range(2):
        lease = queue.claim()
        assert lease is not None and lease.submission_id == "poison"
        clock.advance(11)
        queue.reclaim_expired()
    assert queue.claim() is None
    assert queue.dead_letters() == ["poison"]


def test_a_reclaimed_worker_cannot_complete_the_lease_that_replaced_it() -> None:
    """The fencing token: worker A's late completion must not pop worker B's live lease."""
    clock = FakeClock(start=1_000.0)
    queue = SubmissionQueue(clock=clock, lease_seconds=30.0, max_attempts=3)
    queue.submit("s1")
    stale = queue.claim()
    assert stale is not None
    clock.advance(31)  # worker A is paused past its lease; the reaper hands s1 back
    assert queue.reclaim_expired() == ["s1"]
    live = queue.claim()
    assert live is not None
    assert live.submission_id == stale.submission_id
    assert live.token != stale.token  # every re-claim mints a new token
    assert (stale.attempt, live.attempt) == (1, 2)

    with pytest.raises(ConflictError, match="no longer holds the lease"):
        queue.complete(stale.submission_id, stale.token)  # worker A wakes up
    assert queue.depth() == (0, 1)  # worker B still holds its lease

    queue.complete(live.submission_id, live.token)  # the current holder is unaffected
    assert queue.depth() == (0, 0)


def test_a_lease_that_ran_out_cannot_complete_even_before_the_reaper_runs() -> None:
    clock = FakeClock(start=1_000.0)
    queue = SubmissionQueue(clock=clock, lease_seconds=30.0)
    queue.submit("s1")
    lease = queue.claim()
    assert lease is not None
    clock.advance(31)
    with pytest.raises(ConflictError, match="no longer holds the lease"):
        queue.complete(lease.submission_id, lease.token)


def test_queue_validation_errors() -> None:
    queue = SubmissionQueue(clock=FakeClock())
    queue.submit("s1")
    with pytest.raises(ValidationError):
        queue.submit("s1")
    with pytest.raises(NotFoundError):
        queue.complete("s1", "lt-1")  # claimed by nobody
    with pytest.raises(ValidationError):
        Judge().judge(Program(), [], LIMITS)


def test_concurrent_workers_claim_each_submission_exactly_once() -> None:
    queue = SubmissionQueue(clock=FakeClock(start=0.0), lease_seconds=1_000.0)
    for i in range(400):
        queue.submit(f"s{i}")
    with ThreadPoolExecutor(max_workers=16) as pool:
        leases = [c for c in pool.map(lambda _: queue.claim(), range(500)) if c is not None]
    claimed = [lease.submission_id for lease in leases]
    assert sorted(claimed) == sorted(f"s{i}" for i in range(400))
    assert len(set(claimed)) == 400  # no submission judged twice
    assert len({lease.token for lease in leases}) == 400  # and no token handed out twice
    assert queue.depth() == (0, 400)
