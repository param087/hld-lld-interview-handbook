"""Tests for the thread-safe circuit breaker state machine."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.circuit_breaker import (
    BreakerPolicy,
    CircuitBreaker,
    CircuitOpenError,
    State,
)

FAST = BreakerPolicy(
    failure_rate_threshold=0.5,
    min_calls=4,
    window_seconds=10.0,
    open_seconds=5.0,
    half_open_max_calls=1,
    success_threshold=1,
)


def boom() -> str:
    raise TimeoutError("dependency timed out")


def fine() -> str:
    return "ok"


def drive(breaker: CircuitBreaker, results: str) -> list[str]:
    """Run one call per character: 'x' fails, '.' succeeds; rejections are recorded as 'R'."""
    outcomes: list[str] = []
    for char in results:
        try:
            breaker.call(boom if char == "x" else fine)
            outcomes.append("ok")
        except TimeoutError:
            outcomes.append("failed")
        except CircuitOpenError:
            outcomes.append("R")
    return outcomes


def test_breaker_stays_closed_below_the_minimum_volume_and_below_the_rate() -> None:
    breaker = CircuitBreaker("db", FAST, FakeClock())
    assert drive(breaker, "xxx") == ["failed"] * 3  # 3 failures, min_calls is 4
    assert breaker.state is State.CLOSED
    assert breaker.snapshot().failure_rate == 1.0

    calm = CircuitBreaker("db", FAST, FakeClock())
    drive(calm, "x...x.")  # the rate peaks at 2/5 = 40%, under the 50% threshold
    assert calm.state is State.CLOSED
    assert calm.snapshot().calls == 6
    assert calm.snapshot().failure_rate == pytest.approx(1 / 3)


def test_the_windowed_failure_rate_opens_the_breaker_and_rejections_skip_the_dependency() -> None:
    transitions: list[tuple[str, State, State]] = []
    clock = FakeClock()
    breaker = CircuitBreaker("db", FAST, clock, on_transition=lambda *e: transitions.append(e))
    reached = 0

    def dependency() -> str:
        nonlocal reached
        reached += 1
        raise TimeoutError("dependency timed out")

    for _ in range(4):
        with pytest.raises(TimeoutError):
            breaker.call(dependency)

    assert breaker.state is State.OPEN
    assert transitions == [("db", State.CLOSED, State.OPEN)]
    assert reached == 4

    with pytest.raises(CircuitOpenError) as caught:
        breaker.call(dependency)
    assert reached == 4  # the rejected call never touched the dependency
    assert caught.value.state is State.OPEN
    assert caught.value.retry_after == pytest.approx(5.0)
    clock.advance(3)
    assert breaker.snapshot().retry_after == pytest.approx(2.0)
    assert breaker.snapshot().rejected == 1


def test_half_open_admits_one_trial_at_a_time_and_a_failure_reopens_it() -> None:
    clock = FakeClock()
    policy = BreakerPolicy(
        failure_rate_threshold=0.5,
        min_calls=4,
        open_seconds=5.0,
        half_open_max_calls=1,
        success_threshold=2,
    )
    breaker = CircuitBreaker("db", policy, clock)
    drive(breaker, "xxxx")
    assert breaker.state is State.OPEN

    clock.advance(5)
    assert breaker.state is State.HALF_OPEN  # lazy transition, no timer thread

    permit = breaker.acquire()  # the single trial slot is now taken
    with pytest.raises(CircuitOpenError) as caught:
        breaker.acquire()
    assert caught.value.state is State.HALF_OPEN
    assert caught.value.retry_after == 0.0
    breaker.record(permit, ok=False)
    assert breaker.state is State.OPEN  # one trial failure is enough to reopen

    clock.advance(5)
    assert drive(breaker, ".") == ["ok"]
    assert breaker.state is State.HALF_OPEN  # success_threshold is 2, so one is not enough
    assert drive(breaker, ".") == ["ok"]
    assert breaker.state is State.CLOSED
    assert breaker.snapshot().calls == 0  # closing clears the window


def test_the_sliding_window_forgets_outcomes_older_than_window_seconds() -> None:
    clock = FakeClock()
    policy = BreakerPolicy(failure_rate_threshold=0.5, min_calls=4, window_seconds=10.0)
    breaker = CircuitBreaker("db", policy, clock)

    drive(breaker, "xxx")
    clock.advance(11)  # the three failures fall out of the window
    drive(breaker, "xxx")
    assert breaker.state is State.CLOSED
    assert breaker.snapshot().calls == 3
    drive(breaker, "x")
    assert breaker.state is State.OPEN  # 4 failures inside one window


def test_ignored_exceptions_and_slow_calls() -> None:
    clock = FakeClock()
    policy = BreakerPolicy(
        failure_rate_threshold=0.5,
        min_calls=2,
        slow_call_seconds=1.0,
        ignored_exceptions=(ValidationError,),
    )
    breaker = CircuitBreaker("db", policy, clock)

    def bad_request() -> str:
        raise ValidationError("the caller sent nonsense")

    for _ in range(5):
        with pytest.raises(ValidationError):
            breaker.call(bad_request)
    assert breaker.state is State.CLOSED
    assert breaker.snapshot().calls == 0  # the caller's bug never counts against the dependency

    def slow() -> str:
        clock.advance(1.5)
        return "ok"

    assert breaker.call(slow) == "ok"  # it returned, but late
    assert breaker.snapshot().failures == 1
    assert breaker.call(slow) == "ok"
    assert breaker.state is State.OPEN  # two slow calls trip a 50% rate over min_calls=2


def test_a_permit_from_an_earlier_episode_cannot_corrupt_the_current_one() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("db", FAST, clock)
    stale = breaker.acquire()  # issued while closed
    drive(breaker, "xxxx")
    assert breaker.state is State.OPEN

    breaker.record(stale, ok=False)  # a late timeout from before the trip
    assert breaker.state is State.OPEN
    assert breaker.snapshot().calls == 4  # the window is untouched

    breaker.reset()
    assert breaker.state is State.CLOSED
    assert breaker.snapshot() == breaker.snapshot()
    assert breaker.snapshot().calls == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"failure_rate_threshold": 0.0},
        {"failure_rate_threshold": 1.5},
        {"min_calls": 0},
        {"half_open_max_calls": 0},
        {"success_threshold": 0},
        {"window_seconds": 0.0},
        {"open_seconds": -1.0},
        {"slow_call_seconds": 0.0},
    ],
)
def test_invalid_policies_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        BreakerPolicy(**kwargs)


def test_an_empty_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CircuitBreaker("")


def test_an_open_breaker_rejects_a_concurrent_flood_without_touching_the_dependency() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("db", FAST, clock)
    drive(breaker, "xxxx")
    assert breaker.state is State.OPEN
    reached = 0
    counter_lock = threading.Lock()

    def dependency() -> str:
        nonlocal reached
        with counter_lock:
            reached += 1
        return "ok"

    def hammer(_worker: int) -> int:
        rejected = 0
        for _ in range(100):
            try:
                breaker.call(dependency)
            except CircuitOpenError:
                rejected += 1
        return rejected

    with ThreadPoolExecutor(max_workers=16) as pool:
        rejections = sum(pool.map(hammer, range(16)))

    assert rejections == 1_600
    assert reached == 0
    assert breaker.snapshot().rejected == 1_600


def test_concurrent_half_open_probes_admit_exactly_half_open_max_calls() -> None:
    clock = FakeClock()
    policy = BreakerPolicy(
        failure_rate_threshold=0.5, min_calls=4, open_seconds=5.0, half_open_max_calls=3
    )
    breaker = CircuitBreaker("db", policy, clock)
    drive(breaker, "xxxx")
    clock.advance(5)

    def probe(_worker: int) -> bool:
        try:
            breaker.acquire()
        except CircuitOpenError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=16) as pool:
        admitted = sum(pool.map(probe, range(64)))

    assert admitted == 3  # exactly half_open_max_calls trial slots, however the threads raced
    assert breaker.state is State.HALF_OPEN
    assert breaker.snapshot().rejected == 61
