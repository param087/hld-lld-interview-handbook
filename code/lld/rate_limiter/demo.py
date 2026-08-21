"""A gateway with three rules: the same traffic through the middleware and the five algorithms."""

from __future__ import annotations

from common import FakeClock
from lld.rate_limiter.models import Algorithm, KeyScope, RateLimitRule, Request, Response
from lld.rate_limiter.services import (
    InMemoryStorage,
    LimiterFactory,
    RateLimitMiddleware,
    RuleRegistry,
)
from lld.rate_limiter.strategies import (
    FixedWindowCounter,
    LeakyBucket,
    RateLimiter,
    SlidingWindowCounter,
    SlidingWindowLog,
    TokenBucket,
)

ORDERS = RateLimitRule(
    name="orders-write",
    method="POST",
    path_prefix="/api/orders",
    scope=KeyScope.USER,
    algorithm=Algorithm.TOKEN_BUCKET,
    limit=5,
    window_seconds=60.0,
)
SEARCH = RateLimitRule(
    name="search",
    method="GET",
    path_prefix="/api/search",
    scope=KeyScope.API_KEY,
    algorithm=Algorithm.SLIDING_COUNTER,
    limit=10,
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


def handler(request: Request) -> Response:
    return Response(200, f"ok {request.method} {request.path}")


def algorithms(clock: FakeClock, storage: InMemoryStorage) -> dict[str, RateLimiter]:
    shared = {"capacity": 5, "window_seconds": 1.0, "storage": storage, "clock": clock}
    return {
        "token bucket": TokenBucket(**shared),
        "leaky bucket": LeakyBucket(**shared),
        "fixed window": FixedWindowCounter(**shared),
        "sliding log": SlidingWindowLog(**shared),
        "sliding counter": SlidingWindowCounter(**shared),
    }


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    storage = InMemoryStorage(stripes=8)
    registry = RuleRegistry([ORDERS, SEARCH, CATCH_ALL])
    middleware = RateLimitMiddleware(registry, LimiterFactory(storage, clock=clock))

    print("--- 7 POST /api/orders from user u-1, limit 5 per minute ---")
    order = Request("POST", "/api/orders", client_ip="10.0.0.9", user_id="u-1")
    responses = [middleware(order, handler) for _ in range(7)]
    allowed = sum(r.status == 200 for r in responses)
    print(f"  allowed {allowed}, denied {len(responses) - allowed}, last body: {responses[-1].body}")
    print(f"  429 headers: {dict(responses[-1].headers)}")

    print("--- the same user on a different route uses a different budget ---")
    search = Request("GET", "/api/search", client_ip="10.0.0.9", user_id="u-1", api_key="k-42", cost=5)
    first, second, third = (middleware(search, handler) for _ in range(3))
    print(f"  cost=5 against a limit of 10: {first.status}, {second.status}, then {third.status}")

    print("--- an anonymous request falls through to the IP rule ---")
    anonymous = middleware(Request("GET", "/health", client_ip="10.0.0.9"), handler)
    print(f"  GET /health -> {anonymous.status}, {dict(anonymous.headers)}")

    print("--- limit 5 per second, 7 requests arrive together at t=0 ---")
    for name, limiter in algorithms(clock, storage).items():
        decisions = [limiter.allow(f"burst|{name}") for _ in range(7)]
        ok = sum(d.allowed for d in decisions)
        print(f"  {name:<16}: {ok} allowed, {7 - ok} denied, Retry-After {decisions[-1].retry_after:.2f} s")

    print("--- boundary burst: 5 requests at t+0.9 s, 5 more at t+1.0 s ---")
    base = clock.now()
    for name in ("fixed window", "sliding counter"):
        limiter = algorithms(clock, storage)[name]
        clock.set(base + 0.9)
        early = sum(limiter.allow(f"edge|{name}").allowed for _ in range(5))
        clock.set(base + 1.0)
        late = sum(limiter.allow(f"edge|{name}").allowed for _ in range(5))
        print(f"  {name:<16}: {early} allowed, then {late} more within 0.1 s")

    print("--- housekeeping ---")
    clock.set(1_700_000_000 + 3600)
    print(f"  keys held: {len(storage)}, evicted after 10 min idle: {storage.evict_idle(clock.now() - 600)}")
    counts = middleware.metrics.snapshot()
    print("  " + ", ".join(f"{name}: {c.allowed} allowed / {c.denied} denied" for name, c in sorted(counts.items())))


if __name__ == "__main__":
    main()
