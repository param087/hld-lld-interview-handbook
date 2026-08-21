"""Tests for the backoff, jitter and retry-budget module."""

from __future__ import annotations

import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.retry import (
    TRANSIENT,
    BackoffPolicy,
    Jitter,
    Retrier,
    RetryBudget,
    retry,
    storm_spread,
)


class Recorder:
    """A sleeper that records instead of sleeping, so tests never wait."""

    def __init__(self) -> None:
        self.pauses: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.pauses.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.pauses)


def flaky(fail_times: int, error: type[BaseException] = TimeoutError) -> Callable[[], str]:
    """A callable that raises ``error`` the first ``fail_times`` times, then returns 'ok'."""
    state: dict[str, int] = {"calls": 0}

    def call() -> str:
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise error("transient")
        return "ok"

    return call


def test_unjittered_backoff_doubles_and_stops_at_the_cap() -> None:
    policy = BackoffPolicy(
        base_seconds=0.1, cap_seconds=1.0, multiplier=2.0, max_attempts=8, jitter=Jitter.NONE
    )
    assert list(policy.delays(random.Random(42))) == [0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0]
    assert policy.exponential(1) == pytest.approx(0.1)
    assert policy.exponential(20) == 1.0  # capped, not 100 ms x 2^19 = 14.6 hours
    # max_attempts counts the first call, so 4 attempts is 1 call plus 3 retries
    assert len(list(BackoffPolicy(max_attempts=4, jitter=Jitter.NONE).delays(random.Random(1)))) == 3
    assert list(BackoffPolicy(max_attempts=1).delays(random.Random(1))) == []


@pytest.mark.parametrize("jitter", [Jitter.FULL, Jitter.EQUAL, Jitter.DECORRELATED])
def test_every_jitter_scheme_stays_inside_its_bounds(jitter: Jitter) -> None:
    policy = BackoffPolicy(
        base_seconds=0.1, cap_seconds=10.0, multiplier=2.0, max_attempts=9, jitter=jitter
    )
    rng = random.Random(42)
    for _ in range(200):
        previous = policy.base_seconds
        for attempt, sleep in enumerate(policy.delays(rng), start=1):
            exp = policy.exponential(attempt)
            if jitter is Jitter.FULL:
                assert 0.0 <= sleep <= exp
            elif jitter is Jitter.EQUAL:
                assert exp / 2 <= sleep <= exp
            else:
                assert policy.base_seconds <= sleep <= max(policy.base_seconds, min(10.0, previous * 3))
            assert sleep <= policy.cap_seconds
            previous = sleep


def test_jitter_spreads_a_synchronised_retry_storm() -> None:
    # 200 clients that failed together, all due to retry 400 ms later
    assert storm_spread(200, 0.4, Jitter.NONE, random.Random(42), bucket=0.02) == 200
    spread_full = storm_spread(200, 0.4, Jitter.FULL, random.Random(42), bucket=0.02)
    spread_equal = storm_spread(200, 0.4, Jitter.EQUAL, random.Random(42), bucket=0.02)
    assert spread_full <= 30  # ~200 / 20 windows = 10 expected, plus sampling noise
    assert spread_full < spread_equal < 100  # equal jitter uses half the range, so twice the peak


def test_retry_returns_on_the_first_success_and_sleeps_between_attempts() -> None:
    sleeper = Recorder()
    policy = BackoffPolicy(base_seconds=0.1, max_attempts=4, jitter=Jitter.NONE)
    retrier = Retrier(policy, rng=random.Random(42), sleep=sleeper)

    assert retrier.call(flaky(2)) == "ok"

    assert sleeper.pauses == [0.1, 0.2]  # 2 retries: no sleep after the success
    stats = retrier.stats()
    assert (stats.calls, stats.attempts, stats.failures, stats.retries) == (1, 3, 2, 2)
    assert stats.amplification == pytest.approx(3.0)
    assert stats.slept_seconds == pytest.approx(0.3)
    assert (stats.exhausted, stats.budget_denied) == (0, 0)

    assert retrier.call(flaky(0)) == "ok"  # a success costs no attempts and no sleep
    assert retrier.stats().attempts == 4
    assert sleeper.total == pytest.approx(0.3)


def test_exhausted_attempts_reraise_the_last_error_and_non_retryable_errors_pass_straight_through() -> None:
    sleeper = Recorder()
    policy = BackoffPolicy(base_seconds=0.1, max_attempts=3, jitter=Jitter.NONE)
    retrier = Retrier(policy, retry_on=TRANSIENT, rng=random.Random(42), sleep=sleeper)

    with pytest.raises(TimeoutError):
        retrier.call(flaky(99))
    assert len(sleeper.pauses) == 2  # 3 attempts, 2 sleeps, then the error escapes
    assert retrier.stats().exhausted == 1
    assert retrier.stats().attempts == 3

    with pytest.raises(ValueError):
        retrier.call(flaky(99, ValueError))
    assert retrier.stats().attempts == 4  # one attempt only: a bad request is not transient
    assert retrier.stats().failures == 3
    assert len(sleeper.pauses) == 2


def test_budget_throttles_retries_during_a_total_outage() -> None:
    budget = RetryBudget(max_tokens=100.0, token_ratio=0.1)
    assert budget.tokens == 100.0
    assert budget.allows_retry()
    policy = BackoffPolicy(base_seconds=0.01, max_attempts=4, jitter=Jitter.NONE)
    retrier = Retrier(policy, budget=budget, rng=random.Random(42), sleep=Recorder())

    for _ in range(100):
        with pytest.raises(ConnectionError):
            retrier.call(flaky(999, ConnectionError))

    stats = retrier.stats()
    # 12 calls x 4 attempts drains 100 tokens to 52; the throttle then closes and each further
    # call is a single attempt: 48 + 2 + 87 = 137 attempts instead of 400.
    assert stats.attempts == 137
    assert stats.amplification == pytest.approx(1.37)
    assert stats.budget_denied == 88
    assert budget.tokens == 0.0
    assert not budget.allows_retry()

    # successes refill the bucket at token_ratio, so a healthy dependency retries again
    for _ in range(600):
        budget.record(ok=True)
    assert budget.tokens == pytest.approx(60.0)
    assert budget.allows_retry()
    assert retrier.call(flaky(1, ConnectionError)) == "ok"


def test_decorator_shares_one_retrier_and_its_budget() -> None:
    sleeper = Recorder()
    budget = RetryBudget(max_tokens=10.0, token_ratio=0.1)
    guard = retry(
        BackoffPolicy(base_seconds=0.05, max_attempts=3, jitter=Jitter.NONE),
        retry_on=(TimeoutError,),
        budget=budget,
        rng=random.Random(42),
        sleep=sleeper,
    )

    @guard
    def fetch(user_id: int) -> str:
        if user_id == 0:
            raise TimeoutError("upstream timed out")
        return f"user-{user_id}"

    assert fetch(7) == "user-7"
    assert fetch.__name__ == "fetch"  # functools.wraps keeps the identity
    assert budget.tokens == pytest.approx(10.0)  # already full, a success cannot overfill it
    with pytest.raises(TimeoutError):
        fetch(0)
    assert sleeper.pauses == [0.05, 0.1]
    assert budget.tokens == pytest.approx(7.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_seconds": 0.0},
        {"base_seconds": 1.0, "cap_seconds": 0.5},
        {"multiplier": 0.5},
        {"max_attempts": 0},
    ],
)
def test_invalid_policies_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        BackoffPolicy(**kwargs)


def test_invalid_budget_and_retrier_arguments_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RetryBudget(max_tokens=0)
    with pytest.raises(ValidationError):
        RetryBudget(token_ratio=1.5)
    with pytest.raises(ValidationError):
        Retrier(retry_on=())
    with pytest.raises(ValidationError):
        BackoffPolicy().exponential(0)


def test_concurrent_callers_share_the_budget_and_the_counters() -> None:
    budget = RetryBudget(max_tokens=40.0, token_ratio=0.1)
    policy = BackoffPolicy(base_seconds=0.001, max_attempts=3, jitter=Jitter.FULL)
    retrier = Retrier(policy, budget=budget, rng=random.Random(42), sleep=Recorder())
    workers, per_worker = 8, 25

    def hammer(_worker: int) -> None:
        for _ in range(per_worker):
            with pytest.raises(ConnectionError):
                retrier.call(flaky(999, ConnectionError))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(hammer, range(workers)))

    stats = retrier.stats()
    calls = workers * per_worker
    assert stats.calls == calls
    assert stats.failures == stats.attempts  # every attempt failed
    assert stats.exhausted + stats.budget_denied == calls  # every call ended one way or the other
    # the budget holds 40 tokens and every attempt spends one, so the fleet cannot make more
    # than 40 retries however the threads interleave; without it there would be 2 x 200 = 400
    assert 0 < stats.retries <= 40
    assert budget.tokens == 0.0
