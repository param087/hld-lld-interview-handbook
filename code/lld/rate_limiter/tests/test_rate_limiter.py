from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from common import FakeClock, ValidationError
from lld.rate_limiter.models import (
    Algorithm,
    ClientKey,
    KeyScope,
    RateLimitRule,
    Request,
    Response,
    RuleNotFoundError,
    UnknownAlgorithmError,
)
from lld.rate_limiter.services import (
    DefaultKeyExtractor,
    InMemoryStorage,
    LimiterFactory,
    RateLimitMiddleware,
    RuleRegistry,
)
from lld.rate_limiter.strategies import (
    FixedWindowCounter,
    LeakyBucket,
    SlidingWindowCounter,
    SlidingWindowLog,
    StorageBackedLimiter,
    TokenBucket,
)

ALL_ALGORITHMS = [TokenBucket, LeakyBucket, FixedWindowCounter, SlidingWindowLog, SlidingWindowCounter]

ORDERS = RateLimitRule(
    name="orders-write",
    method="POST",
    path_prefix="/api/orders",
    scope=KeyScope.USER,
    algorithm=Algorithm.TOKEN_BUCKET,
    limit=3,
    window_seconds=60.0,
)
CATCH_ALL = RateLimitRule(
    name="catch-all",
    method="*",
    path_prefix="/",
    scope=KeyScope.IP,
    algorithm=Algorithm.FIXED_WINDOW,
    limit=100,
    window_seconds=60.0,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000)


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage(stripes=8)


def build(kind: type[StorageBackedLimiter], clock: FakeClock, storage: InMemoryStorage, capacity: int = 5):
    return kind(capacity=capacity, window_seconds=1.0, storage=storage, clock=clock, rule=kind.__name__)


def ok(request: Request) -> Response:
    return Response(200, "ok")


def test_token_bucket_allows_a_burst_then_the_sustained_rate(clock: FakeClock, storage: InMemoryStorage) -> None:
    limiter = build(TokenBucket, clock, storage)  # 5 tokens, refilled 5 per second
    assert sum(limiter.allow("u-1").allowed for _ in range(5)) == 5
    denied = limiter.allow("u-1")
    assert not denied.allowed and denied.remaining == 0
    assert denied.retry_after == pytest.approx(0.2)  # one token at 5 per second
    assert denied.headers()["Retry-After"] == "1"  # never 0: a client reading 0 retries at once
    clock.advance(0.4)  # two tokens back
    assert sum(limiter.allow("u-1").allowed for _ in range(3)) == 2


@pytest.mark.parametrize("kind", ALL_ALGORITHMS, ids=lambda k: k.__name__)
def test_every_algorithm_admits_exactly_the_limit_in_one_burst(
    kind: type[StorageBackedLimiter], clock: FakeClock, storage: InMemoryStorage
) -> None:
    limiter = build(kind, clock, storage)
    decisions = [limiter.allow("u-1") for _ in range(7)]
    assert sum(d.allowed for d in decisions) == 5
    assert all(d.retry_after > 0 for d in decisions if not d.allowed)


# --8<-- [start:boundary]
def test_fixed_window_leaks_at_the_boundary_and_the_sliding_counter_does_not(
    clock: FakeClock, storage: InMemoryStorage
) -> None:
    """The one comparison every interviewer asks for, in six lines."""
    fixed = build(FixedWindowCounter, clock, storage)
    sliding = build(SlidingWindowCounter, clock, storage)
    clock.set(1_000_000.9)  # late in the window
    assert sum(fixed.allow("u-1").allowed for _ in range(5)) == 5
    assert sum(sliding.allow("u-1").allowed for _ in range(5)) == 5
    clock.set(1_000_001.0)  # the next window starts
    assert sum(fixed.allow("u-1").allowed for _ in range(5)) == 5  # 10 in 100 ms
    assert sum(sliding.allow("u-1").allowed for _ in range(5)) == 0  # the overlap still counts


# --8<-- [end:boundary]


def test_sliding_log_retry_after_points_at_the_oldest_entry(clock: FakeClock, storage: InMemoryStorage) -> None:
    limiter = build(SlidingWindowLog, clock, storage, capacity=2)
    limiter.allow("u-1")
    clock.advance(0.3)
    limiter.allow("u-1")
    denied = limiter.allow("u-1")
    assert not denied.allowed
    assert denied.retry_after == pytest.approx(0.7)  # the first entry ages out 0.7 s from now


def test_a_rule_may_never_be_satisfiable_by_a_single_request(clock: FakeClock, storage: InMemoryStorage) -> None:
    limiter = build(TokenBucket, clock, storage, capacity=5)
    with pytest.raises(ValidationError):
        limiter.allow("u-1", cost=6)
    with pytest.raises(ValidationError):
        limiter.allow("u-1", cost=0)


@pytest.mark.parametrize(
    "overrides",
    [{"limit": 0}, {"window_seconds": 0.0}, {"burst": 0}, {"path_prefix": "api/orders"}],
)
def test_rule_validation_rejects_impossible_configuration(overrides: dict[str, object]) -> None:
    fields = {
        "name": "bad",
        "method": "GET",
        "path_prefix": "/x",
        "scope": KeyScope.IP,
        "algorithm": Algorithm.FIXED_WINDOW,
        "limit": 1,
        "window_seconds": 1.0,
        **overrides,
    }
    with pytest.raises(ValidationError):
        RateLimitRule(**fields)  # type: ignore[arg-type]


def test_registry_prefers_the_most_specific_rule_and_reports_a_gap() -> None:
    registry = RuleRegistry([CATCH_ALL, ORDERS])
    assert registry.rule_for("POST", "/api/orders").name == "orders-write"
    assert registry.rule_for("GET", "/api/orders").name == "catch-all"  # method does not match
    assert RuleRegistry([ORDERS]).rules()[0] is ORDERS
    with pytest.raises(RuleNotFoundError):
        RuleRegistry([ORDERS]).rule_for("GET", "/health")


def test_two_rules_never_share_a_budget_for_the_same_caller(
    clock: FakeClock, storage: InMemoryStorage
) -> None:
    key = ClientKey(KeyScope.USER, "u-1").storage_key()
    strict = TokenBucket(capacity=2, window_seconds=60.0, storage=storage, clock=clock, rule="orders-write")
    loose = TokenBucket(capacity=50, window_seconds=60.0, storage=storage, clock=clock, rule="catch-all")
    assert sum(strict.allow(key).allowed for _ in range(4)) == 2
    assert sum(loose.allow(key).allowed for _ in range(4)) == 4  # untouched by the strict rule
    assert len(storage) == 2  # one record per rule, not one per caller


def test_anonymous_requests_fall_back_to_the_ip_key() -> None:
    extractor = DefaultKeyExtractor()
    signed_in = Request("POST", "/api/orders", client_ip="10.0.0.1", user_id="u-1")
    anonymous = Request("POST", "/api/orders", client_ip="10.0.0.1")
    assert extractor.extract(signed_in, KeyScope.USER) == ClientKey(KeyScope.USER, "u-1")
    assert extractor.extract(anonymous, KeyScope.USER) == ClientKey(KeyScope.IP, "10.0.0.1")


def test_middleware_answers_429_with_headers_and_records_metrics(clock: FakeClock, storage: InMemoryStorage) -> None:
    middleware = RateLimitMiddleware(RuleRegistry([ORDERS, CATCH_ALL]), LimiterFactory(storage, clock=clock))
    request = Request("POST", "/api/orders", client_ip="10.0.0.1", user_id="u-1")
    statuses = [middleware(request, ok).status for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    last = middleware(request, ok)
    assert last.headers["Retry-After"] == "20"  # 3 per 60 s is one token every 20 s
    counters = middleware.metrics.snapshot()["orders-write"]
    assert (counters.allowed, counters.denied) == (3, 2)


def test_cost_weighted_requests_spend_more_of_the_budget(clock: FakeClock, storage: InMemoryStorage) -> None:
    middleware = RateLimitMiddleware(RuleRegistry([CATCH_ALL]), LimiterFactory(storage, clock=clock))
    cheap = Request("GET", "/api/search", client_ip="10.0.0.2")
    heavy = Request("GET", "/api/search", client_ip="10.0.0.2", cost=99)
    assert middleware(heavy, ok).status == 200
    assert middleware(cheap, ok).status == 200
    assert middleware(cheap, ok).status == 429  # 99 + 1 exhausts the limit of 100
    with pytest.raises(ValidationError):
        Request("GET", "/x", cost=0)


def test_hot_reload_raises_the_limit_without_a_restart(clock: FakeClock, storage: InMemoryStorage) -> None:
    registry = RuleRegistry([ORDERS])
    factory = LimiterFactory(storage, clock=clock)
    middleware = RateLimitMiddleware(registry, factory)
    request = Request("POST", "/api/orders", client_ip="10.0.0.1", user_id="u-1")
    assert [middleware(request, ok).status for _ in range(4)] == [200, 200, 200, 429]

    registry.replace([replace(ORDERS, limit=10)])
    denied = middleware(request, ok)
    assert denied.headers["X-RateLimit-Limit"] == "10"  # the new ceiling is already live
    clock.advance(6)  # tokens now arrive at 10 per minute, not 3
    assert middleware(request, ok).status == 200
    assert factory.prune(registry.rules()) == 1  # the limiter built from the old rule is dropped


def test_unknown_algorithm_is_rejected(storage: InMemoryStorage) -> None:
    class EmptyFactory(LimiterFactory):
        BUILDERS: dict[Algorithm, type[StorageBackedLimiter]] = {}

    with pytest.raises(UnknownAlgorithmError):
        EmptyFactory(storage).for_rule(ORDERS)


# --8<-- [start:concurrency]
def test_concurrent_requests_never_exceed_the_limit(clock: FakeClock, storage: InMemoryStorage) -> None:
    """8 threads, 400 requests, a frozen clock, capacity 100: exactly 100 pass.

    Without the stripe lock, "read the level, subtract the cost, write it back" is
    three steps, and two threads that interleave them both spend the same token.
    The frozen clock removes refill from the picture, so any number above 100
    is a lost update rather than time passing.
    """
    limiter = build(TokenBucket, clock, storage, capacity=100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.allow("hot-tenant").allowed, range(400)))
    assert sum(results) == 100
    assert len(storage) == 1  # one key, one stripe, one contended lock


def test_independent_keys_do_not_interfere(clock: FakeClock, storage: InMemoryStorage) -> None:
    limiter = build(TokenBucket, clock, storage, capacity=10)

    def hammer(tenant: int) -> int:
        return sum(limiter.allow(f"tenant-{tenant}").allowed for _ in range(20))

    with ThreadPoolExecutor(max_workers=8) as pool:
        allowed = list(pool.map(hammer, range(8)))
    assert allowed == [10] * 8 and len(storage) == 8


# --8<-- [end:concurrency]


def test_idle_keys_are_evicted_so_the_map_cannot_grow_forever(
    clock: FakeClock, storage: InMemoryStorage
) -> None:
    limiter = build(TokenBucket, clock, storage, capacity=10)
    for tenant in range(20):
        limiter.allow(f"tenant-{tenant}")
    clock.advance(600)
    limiter.allow("tenant-0")  # one caller is still active
    assert storage.evict_idle(clock.now() - 60) == 19
    assert storage.keys() == ["TokenBucket|tenant-0"] and len(storage) == 1
