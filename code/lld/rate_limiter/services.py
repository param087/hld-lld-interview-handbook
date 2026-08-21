"""Storage, rule registry, limiter factory, metrics and the middleware.

Every lock in this package is in this file: the storage stripes and the two that
guard configuration and counters.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Protocol

from common import Clock, SystemClock, ValidationError
from lld.rate_limiter.models import (
    Algorithm,
    ClientKey,
    Decision,
    KeyScope,
    LimiterState,
    RateLimitRule,
    Request,
    Response,
    RuleCounters,
    RuleNotFoundError,
    UnknownAlgorithmError,
)
from lld.rate_limiter.strategies import (
    FixedWindowCounter,
    LeakyBucket,
    Mutation,
    RateLimiter,
    SlidingWindowCounter,
    SlidingWindowLog,
    Storage,
    StorageBackedLimiter,
    TokenBucket,
)

DEFAULT_STRIPES = 64
TOO_MANY_REQUESTS = 429

type Handler = Callable[[Request], Response]


# --8<-- [start:storage]
class InMemoryStorage:
    """Process-local limiter state behind striped locks: one lock *and one dict* per stripe.

    Striping is the answer to "one lock for the whole gateway is a bottleneck".
    Two keys that land on different stripes never wait for each other, so
    contention falls roughly with the stripe count, while requests for the *same*
    key still serialise - which is what you want, because the hot key is exactly
    the one that has to be counted correctly.

    Each stripe owns its own dict on purpose. One shared dict mutated under
    several different locks would be relying on CPython's internals for safety
    rather than on a lock, and that is not a claim you want to defend.
    """

    def __init__(self, stripes: int = DEFAULT_STRIPES) -> None:
        if stripes < 1:
            raise ValidationError("stripes must be at least 1")
        self._locks = [threading.Lock() for _ in range(stripes)]
        self._states: list[dict[str, LimiterState]] = [{} for _ in range(stripes)]

    def apply(self, key: str, now: float, mutate: Mutation) -> Decision:
        index = hash(key) % len(self._locks)
        with self._locks[index]:
            states = self._states[index]
            record, decision = mutate(states.get(key))
            record.updated_at = now  # the storage stamps it, so no algorithm can forget to
            states[key] = record
            return decision

    def evict_idle(self, cutoff: float) -> int:
        """Drop keys untouched since ``cutoff``. In Redis this is a key TTL instead."""
        removed = 0
        for index, lock in enumerate(self._locks):
            with lock:
                states = self._states[index]
                stale = [key for key, state in states.items() if state.updated_at <= cutoff]
                for key in stale:
                    del states[key]
                removed += len(stale)
        return removed

    def __len__(self) -> int:
        return sum(len(states) for states in self._states)

    def keys(self) -> list[str]:
        out: list[str] = []
        for index, lock in enumerate(self._locks):
            with lock:
                out.extend(self._states[index])
        return out


# --8<-- [end:storage]


# --8<-- [start:registry]
class RuleRegistry:
    """Which rule covers a request, and the seam that makes configuration hot-reloadable.

    ``_lock`` guards one thing: the reference to an immutable tuple of rules. A
    reader copies that reference under the lock and then scans outside it, so a
    reload publishes a whole ruleset at once and no request can ever observe a
    half-applied configuration. Copy-on-write beats locking the read path.
    """

    def __init__(self, rules: Iterable[RateLimitRule] = (), default: RateLimitRule | None = None) -> None:
        self._lock = threading.Lock()
        self._rules = self._ordered(rules)
        self._default = default

    def rules(self) -> tuple[RateLimitRule, ...]:
        with self._lock:
            return self._rules

    def replace(self, rules: Iterable[RateLimitRule]) -> None:
        """Hot reload: swap the whole ruleset in one assignment."""
        ordered = self._ordered(rules)
        with self._lock:
            self._rules = ordered

    def rule_for(self, method: str, path: str) -> RateLimitRule:
        for rule in self.rules():  # already sorted most specific first
            if rule.matches(method, path):
                return rule
        if self._default is not None:
            return self._default
        raise RuleNotFoundError(f"no rate limit rule matches {method} {path}")

    @staticmethod
    def _ordered(rules: Iterable[RateLimitRule]) -> tuple[RateLimitRule, ...]:
        return tuple(sorted(rules, key=lambda r: r.specificity(), reverse=True))


class KeyExtractor(Protocol):
    """Turns a request into the identity a rule counts against."""

    def extract(self, request: Request, scope: KeyScope) -> ClientKey: ...


class DefaultKeyExtractor:
    """User id, API key or client IP, with the IP as the fallback for anonymous callers.

    The fallback is a real decision: keying every anonymous request as ``user:none``
    would give all of them one shared budget, so a single script could exhaust the
    quota of every logged-out visitor at once.
    """

    def extract(self, request: Request, scope: KeyScope) -> ClientKey:
        if scope is KeyScope.USER and request.user_id:
            return ClientKey(KeyScope.USER, request.user_id)
        if scope is KeyScope.API_KEY and request.api_key:
            return ClientKey(KeyScope.API_KEY, request.api_key)
        if scope is KeyScope.GLOBAL:
            return ClientKey(KeyScope.GLOBAL, "all")
        return ClientKey(KeyScope.IP, request.client_ip)


# --8<-- [end:registry]


# --8<-- [start:factory]
class LimiterFactory:
    """Factory: a rule names an algorithm, this turns it into an object - once.

    The cache is keyed by the rule *value*, not by its name. A frozen dataclass
    hashes by its fields, so editing a limit produces a different key and a fresh
    limiter automatically: no invalidation logic, and no stale ceiling surviving
    a hot reload.
    """

    BUILDERS: dict[Algorithm, type[StorageBackedLimiter]] = {
        Algorithm.TOKEN_BUCKET: TokenBucket,
        Algorithm.LEAKY_BUCKET: LeakyBucket,
        Algorithm.FIXED_WINDOW: FixedWindowCounter,
        Algorithm.SLIDING_LOG: SlidingWindowLog,
        Algorithm.SLIDING_COUNTER: SlidingWindowCounter,
    }

    def __init__(self, storage: Storage, clock: Clock | None = None) -> None:
        self._storage = storage
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._limiters: dict[RateLimitRule, RateLimiter] = {}

    def for_rule(self, rule: RateLimitRule) -> RateLimiter:
        with self._lock:
            limiter = self._limiters.get(rule)
            if limiter is None:
                limiter = self._limiters[rule] = self._build(rule)
            return limiter

    def prune(self, active: Iterable[RateLimitRule]) -> int:
        """Drop limiters for rules that are no longer configured."""
        keep = set(active)
        with self._lock:
            dead = [rule for rule in self._limiters if rule not in keep]
            for rule in dead:
                del self._limiters[rule]
            return len(dead)

    def _build(self, rule: RateLimitRule) -> RateLimiter:
        try:
            builder = self.BUILDERS[rule.algorithm]
        except KeyError as exc:
            raise UnknownAlgorithmError(f"no limiter for algorithm {rule.algorithm!r}") from exc
        return builder(
            capacity=rule.capacity,
            window_seconds=rule.window_seconds,
            storage=self._storage,
            clock=self._clock,
            rule=rule.name,
        )


# --8<-- [end:factory]


# --8<-- [start:middleware]
class RateLimitMetrics:
    """Allowed and denied counts per rule, behind their own lock.

    Deliberately *not* the storage lock: a metrics update must never run inside
    the critical section that every request for a hot key already serialises on.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, RuleCounters] = {}

    def record(self, rule_name: str, decision: Decision) -> None:
        with self._lock:
            current = self._counters.get(rule_name, RuleCounters())
            self._counters[rule_name] = RuleCounters(
                allowed=current.allowed + (1 if decision.allowed else 0),
                denied=current.denied + (0 if decision.allowed else 1),
            )

    def snapshot(self) -> dict[str, RuleCounters]:
        with self._lock:
            return dict(self._counters)


class RateLimitMiddleware:
    """One stage of the request pipeline: find the rule, build the key, ask, then pass or 429.

    It is a middleware rather than a decorator on each handler because the limit
    belongs to the route, not to the function: adding an endpoint should not mean
    remembering to annotate it.
    """

    def __init__(
        self,
        registry: RuleRegistry,
        factory: LimiterFactory,
        extractor: KeyExtractor | None = None,
        metrics: RateLimitMetrics | None = None,
    ) -> None:
        self._registry = registry
        self._factory = factory
        self._extractor = extractor or DefaultKeyExtractor()
        self.metrics = metrics or RateLimitMetrics()

    def __call__(self, request: Request, next_handler: Handler) -> Response:
        try:
            rule = self._registry.rule_for(request.method, request.path)
        except RuleNotFoundError:
            return next_handler(request)  # unlimited routes are a configuration choice
        key = self._extractor.extract(request, rule.scope)
        decision = self._factory.for_rule(rule).allow(key.storage_key(), cost=request.cost)
        self.metrics.record(rule.name, decision)
        if not decision.allowed:
            return Response(TOO_MANY_REQUESTS, f"rate limit exceeded for rule {rule.name}", decision.headers())
        return next_handler(request).with_headers(decision.headers())


# --8<-- [end:middleware]
