import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.distributed_rate_limiter import (
    FakeRedis,
    RedisSlidingWindow,
    Rule,
    RuleSet,
    TwoTierLimiter,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=0.0)


def build(clock: FakeClock, limit: int = 5, window: float = 1.0) -> RedisSlidingWindow:
    rule = Rule(scope="test", limit=limit, window=window)
    return RedisSlidingWindow(FakeRedis(clock=clock), rule, clock=clock)


def test_allows_exactly_the_limit_and_reports_a_429_payload(clock: FakeClock) -> None:
    limiter = build(clock)
    decisions = [limiter.allow("ann") for _ in range(7)]
    assert [d.allowed for d in decisions] == [True] * 5 + [False] * 2
    assert decisions[4].remaining == 0
    rejected = decisions[-1]
    assert rejected.headers() == {
        "X-RateLimit-Limit": "5",
        "X-RateLimit-Remaining": "0",
        "Retry-After": "1",
    }
    assert 0 < rejected.retry_after <= 1.0


def test_no_boundary_burst_when_the_window_rolls_over(clock: FakeClock) -> None:
    limiter = build(clock)
    clock.set(0.5)
    assert sum(limiter.allow("bob").allowed for _ in range(5)) == 5
    clock.set(1.0)  # a fixed window would reset here and pass 5 more
    assert sum(limiter.allow("bob").allowed for _ in range(5)) == 0
    clock.set(1.5)  # half the previous window has aged out: 5 * 0.5 = 2.5 used
    assert sum(limiter.allow("bob").allowed for _ in range(5)) == 2


def test_keys_are_isolated_and_expire(clock: FakeClock) -> None:
    limiter = build(clock)
    assert sum(limiter.allow("ann").allowed for _ in range(5)) == 5
    assert limiter.allow("bob").allowed  # a different key has its own counters
    clock.set(10.0)  # both windows are long gone
    assert sum(limiter.allow("ann").allowed for _ in range(5)) == 5


@pytest.mark.parametrize("cost", [0, -1, 6])
def test_invalid_cost_is_rejected(clock: FakeClock, cost: int) -> None:
    with pytest.raises(ValidationError):
        build(clock).allow("ann", cost)


def test_invalid_rules_and_chunks_are_rejected(clock: FakeClock) -> None:
    with pytest.raises(ValidationError):
        Rule(scope="bad", limit=0, window=1.0)
    with pytest.raises(ValidationError):
        Rule(scope="bad", limit=5, window=0.0)
    limiter = build(clock)
    with pytest.raises(ValidationError):
        TwoTierLimiter(limiter, limiter.rule, chunk=99, clock=clock)


def test_read_then_write_over_admits_while_the_atomic_script_does_not(clock: FakeClock) -> None:
    """Both limiters see eight concurrent gateways; only the atomic one holds the limit."""
    barrier = threading.Barrier(8)

    def hold_after_read(command: str) -> None:
        if command == "mget":
            barrier.wait(timeout=5.0)  # pin the interleaving: all read before any writes

    rule = Rule(scope="race", limit=5, window=1.0)
    racy = RedisSlidingWindow(
        FakeRedis(clock=clock, after_command=hold_after_read), rule, clock=clock
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        racy_allowed = sum(pool.map(lambda _: racy.allow_racy("cat").allowed, range(8)))
    assert racy_allowed == 8  # three requests too many

    atomic = RedisSlidingWindow(FakeRedis(clock=clock), rule, clock=clock)
    with ThreadPoolExecutor(max_workers=8) as pool:
        atomic_allowed = sum(pool.map(lambda _: atomic.allow("cat").allowed, range(8)))
    assert atomic_allowed == 5


def test_concurrent_gateways_never_exceed_the_limit(clock: FakeClock) -> None:
    limiter = build(clock, limit=200, window=60.0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        allowed = sum(pool.map(lambda _: limiter.allow("shared").allowed, range(1_000)))
    assert allowed == 200


def test_two_tier_cuts_round_trips_without_exceeding_the_global_limit(clock: FakeClock) -> None:
    rule = Rule(scope="pro", limit=100, window=60.0)
    redis = FakeRedis(clock=clock)
    shared = RedisSlidingWindow(redis, rule, clock=clock)
    nodes = [TwoTierLimiter(shared, rule, chunk=25, clock=clock) for _ in range(4)]
    allowed = sum(nodes[i % 4].allow("tenant").allowed for i in range(160))
    assert allowed == 100  # the global budget, not a byte more
    assert sum(node.round_trips for node in nodes) < 20  # vs 160 without the local tier
    assert redis.commands == sum(node.round_trips for node in nodes)


def test_two_tier_drops_a_reservation_when_the_window_rolls_over(clock: FakeClock) -> None:
    rule = Rule(scope="pro", limit=100, window=60.0)
    shared = RedisSlidingWindow(FakeRedis(clock=clock), rule, clock=clock)
    node = TwoTierLimiter(shared, rule, chunk=25, clock=clock)
    assert node.allow("tenant").allowed
    assert node.reserved == 24  # 25 reserved, 1 spent
    clock.set(120.0)
    assert node.allow("tenant").allowed
    assert node.reserved == 24  # the stale reservation was discarded, not carried over


def test_rules_reload_atomically(clock: FakeClock) -> None:
    default = Rule("anon", 60, 60.0)
    rules = RuleSet([Rule("free", 5, 1.0), Rule("pro", 1_000, 1.0)], default=default)
    assert rules.resolve("free").limit == 5
    assert rules.resolve("nobody") is default  # unknown scopes fall back
    assert rules.version == 1

    snapshot = rules.resolve("free")
    rules.reload([Rule("free", 20, 1.0)])
    assert rules.version == 2
    assert rules.resolve("free").limit == 20
    assert snapshot.limit == 5  # the old rule a request already resolved is untouched
    assert rules.resolve("pro") is default  # a rule dropped from the file falls back
