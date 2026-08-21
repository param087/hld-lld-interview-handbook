from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.rate_limiters import (
    FixedWindowCounter,
    LeakyBucket,
    RateLimiter,
    SlidingWindowCounter,
    SlidingWindowLog,
    TokenBucket,
)

NAMES = ["token bucket", "leaky bucket", "fixed window", "sliding log", "sliding counter"]


def make(name: str, limit: int, clock: FakeClock) -> RateLimiter:
    """``limit`` requests per second for every algorithm (rate == capacity for the buckets)."""
    limiters: dict[str, RateLimiter] = {
        "token bucket": TokenBucket(rate=limit, capacity=limit, clock=clock),
        "leaky bucket": LeakyBucket(rate=limit, capacity=limit, clock=clock),
        "fixed window": FixedWindowCounter(limit=limit, window=1.0, clock=clock),
        "sliding log": SlidingWindowLog(limit=limit, window=1.0, clock=clock),
        "sliding counter": SlidingWindowCounter(limit=limit, window=1.0, clock=clock),
    }
    return limiters[name]


def test_token_bucket_bursts_then_refills_at_the_rate() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2, capacity=4, clock=clock)
    decisions = [bucket.allow("k") for _ in range(5)]
    assert [d.allowed for d in decisions] == [True] * 4 + [False]
    assert [d.remaining for d in decisions] == [3, 2, 1, 0, 0]
    assert decisions[-1].retry_after == pytest.approx(0.5)  # one token at 2 per second
    clock.advance(0.5)
    assert bucket.allow("k").allowed
    assert not bucket.allow("k").allowed
    clock.advance(60)  # the refill is capped at the capacity
    assert [bucket.allow("k").allowed for _ in range(5)] == [True] * 4 + [False]
    assert bucket.allow("other").allowed  # keys are independent
    expensive = bucket.allow("big", cost=4)
    assert expensive.allowed and expensive.remaining == 0
    with pytest.raises(ValidationError):
        bucket.allow("k", cost=5)
    with pytest.raises(ValidationError):
        bucket.allow("k", cost=0)


def test_leaky_bucket_shapes_a_burst_into_a_constant_outflow() -> None:
    clock = FakeClock()
    bucket = LeakyBucket(rate=5, capacity=5, clock=clock)
    decisions = [bucket.allow("k") for _ in range(7)]
    assert [d.allowed for d in decisions] == [True] * 5 + [False] * 2
    assert [d.delay for d in decisions[:5]] == pytest.approx([0.0, 0.2, 0.4, 0.6, 0.8])
    assert decisions[5].retry_after == pytest.approx(0.2)
    clock.advance(0.2)
    admitted = bucket.allow("k")
    assert admitted.allowed and admitted.delay == pytest.approx(0.8)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("token bucket", [5, 0, 3]),
        ("leaky bucket", [5, 0, 3]),
        ("fixed window", [5, 5, 0]),
        ("sliding log", [5, 0, 0]),
        ("sliding counter", [5, 0, 2]),
    ],
)
def test_boundary_burst_per_algorithm(name: str, expected: list[int]) -> None:
    clock = FakeClock()
    limiter = make(name, 5, clock)
    counts: list[int] = []
    for at in (0.9, 1.0, 1.5):
        clock.set(at)
        counts.append(sum(limiter.allow("k").allowed for _ in range(5)))
    assert counts == expected


def test_fixed_window_retry_after_points_at_the_next_window() -> None:
    clock = FakeClock(start=100.0)  # the aligned window is 60-120
    limiter = FixedWindowCounter(limit=2, window=60, clock=clock)
    assert limiter.allow("k").allowed and limiter.allow("k").allowed
    rejected = limiter.allow("k")
    assert not rejected.allowed and rejected.retry_after == pytest.approx(20.0)
    assert rejected.headers() == {
        "X-RateLimit-Limit": "2",
        "X-RateLimit-Remaining": "0",
        "Retry-After": "20",
    }
    clock.advance(20)
    assert limiter.allow("k").allowed
    assert "Retry-After" not in limiter.allow("k").headers()


def test_sliding_log_is_exact_and_waits_for_the_oldest_entries() -> None:
    clock = FakeClock()
    log = SlidingWindowLog(limit=3, window=10, clock=clock)
    for at in (0, 2, 4):
        clock.set(at)
        assert log.allow("k").allowed
    clock.set(5)
    rejected = log.allow("k")
    assert not rejected.allowed and rejected.retry_after == pytest.approx(5.0)
    clock.set(10)
    assert log.allow("k").allowed  # the entry from t=0 has aged out
    assert not log.allow("k").allowed
    clock.set(11)  # entries at 2, 4, 10: a cost of 2 needs the two oldest gone, at t=14
    rejected = log.allow("k", cost=2)
    assert not rejected.allowed and rejected.retry_after == pytest.approx(3.0)


def test_sliding_counter_weights_the_previous_window() -> None:
    clock = FakeClock()
    counter = SlidingWindowCounter(limit=10, window=10, clock=clock)
    clock.set(5)
    assert all(counter.allow("k").allowed for _ in range(10))
    clock.set(14)  # 40% into the next window: estimate = 10 x 0.6 = 6, room for 4
    assert sum(counter.allow("k").allowed for _ in range(6)) == 4
    rejected = counter.allow("k")
    assert rejected.retry_after == pytest.approx(1.0)  # at t=15 the estimate is 5 + 4
    clock.set(15)
    assert counter.allow("k").allowed
    clock.set(31)  # a whole window skipped: nothing carries over
    assert sum(counter.allow("k").allowed for _ in range(11)) == 10


def test_constructor_validation() -> None:
    with pytest.raises(ValidationError):
        TokenBucket(rate=0, capacity=1)
    with pytest.raises(ValidationError):
        LeakyBucket(rate=1, capacity=0)
    with pytest.raises(ValidationError):
        FixedWindowCounter(limit=0, window=1)
    with pytest.raises(ValidationError):
        SlidingWindowLog(limit=1, window=0)
    with pytest.raises(ValidationError):
        SlidingWindowCounter(limit=-1, window=1)


@pytest.mark.parametrize("name", NAMES)
def test_every_limiter_admits_exactly_the_limit_under_concurrency(name: str) -> None:
    limiter = make(name, 100, FakeClock())  # a frozen clock: no refill, no new window
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.allow("k").allowed, range(800)))
    assert sum(results) == 100
    assert not limiter.allow("k").allowed
