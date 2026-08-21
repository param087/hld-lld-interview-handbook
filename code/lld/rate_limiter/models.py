"""Vocabulary of the rate limiter: what a rule is, what a caller is, what an answer is.

Nothing here decides anything. The algorithms live in ``strategies.py`` and the
wiring - registry, factory, middleware, metrics - lives in ``services.py``.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from common import NotFoundError, ValidationError

WILDCARD = "*"


# --8<-- [start:enums]
class Algorithm(StrEnum):
    TOKEN_BUCKET = "token_bucket"  # bursty, smooth refill, two numbers per key
    LEAKY_BUCKET = "leaky_bucket"  # shapes traffic: admits at a constant rate
    FIXED_WINDOW = "fixed_window"  # cheapest, leaks 2x the limit at a boundary
    SLIDING_LOG = "sliding_log"  # exact, O(limit) memory per key
    SLIDING_COUNTER = "sliding_counter"  # approximate, O(1) memory, no boundary burst


class KeyScope(StrEnum):
    """What "one caller" means for a rule. Two rules may scope the same request differently."""

    USER = "user"
    API_KEY = "api_key"
    IP = "ip"
    GLOBAL = "global"


class RuleNotFoundError(NotFoundError):
    """No rule matches the request and the registry has no default."""


class UnknownAlgorithmError(ValidationError):
    """A rule names an algorithm the factory cannot build."""


# --8<-- [end:enums]


# --8<-- [start:rule]
@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """One configured limit: which requests it covers, per whom, how much.

    ``burst`` only means something to the token bucket: it is the capacity, so
    ``limit=60, window_seconds=60, burst=10`` reads as "one request per second on
    average, but ten may arrive at once".
    """

    name: str
    method: str  # "GET", "POST", or "*"
    path_prefix: str
    scope: KeyScope
    algorithm: Algorithm
    limit: int
    window_seconds: float
    burst: int | None = None

    def __post_init__(self) -> None:
        if self.limit <= 0 or self.window_seconds <= 0:
            raise ValidationError(f"rule {self.name}: limit and window must be positive")
        if self.burst is not None and self.burst <= 0:
            raise ValidationError(f"rule {self.name}: burst must be positive")
        if not self.path_prefix.startswith("/"):
            raise ValidationError(f"rule {self.name}: path_prefix must start with '/'")

    @property
    def capacity(self) -> int:
        """What a single request is measured against: the burst for buckets, the limit otherwise."""
        return self.burst or self.limit

    @property
    def refill_rate(self) -> float:
        """Tokens per second. 60 per minute is 1.0 per second."""
        return self.limit / self.window_seconds

    def matches(self, method: str, path: str) -> bool:
        method_ok = self.method in (WILDCARD, method)
        return method_ok and path.startswith(self.path_prefix)

    def specificity(self) -> tuple[int, int]:
        """Sort key: the longest path wins, an exact method beats a wildcard."""
        return len(self.path_prefix), 0 if self.method == WILDCARD else 1


@dataclass(frozen=True, slots=True)
class ClientKey:
    """Who is being limited: a scope and the identity inside it.

    It deliberately does *not* know which rule is asking. The limiter prefixes its
    own rule name, so a caller cannot forget to namespace and two rules covering
    the same user - a strict one on ``POST /orders``, a loose one on everything -
    can never end up sharing a counter.
    """

    scope: KeyScope
    value: str

    def storage_key(self) -> str:
        return f"{self.scope}|{self.value}"


# --8<-- [end:rule]


# --8<-- [start:decision]
@dataclass(frozen=True, slots=True)
class Decision:
    """The answer to one request, carrying everything an HTTP response needs."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: float = 0.0  # seconds until a request of this cost would pass
    rule: str = ""
    delay: float = 0.0  # shaping limiters only: how long the admitted request waits

    def headers(self) -> dict[str, str]:
        out = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
        }
        if not self.allowed:
            # Retry-After is an integer number of seconds, and never 0: a client
            # that reads 0 retries immediately and is denied again.
            out["Retry-After"] = str(max(1, math.ceil(self.retry_after)))
        return out


@dataclass(frozen=True, slots=True)
class RuleCounters:
    allowed: int = 0
    denied: int = 0

    @property
    def total(self) -> int:
        return self.allowed + self.denied

    @property
    def denied_ratio(self) -> float:
        return self.denied / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class Request:
    """The slice of an HTTP request this middleware needs. Frozen: stages copy, never mutate."""

    method: str
    path: str
    client_ip: str = "0.0.0.0"
    user_id: str | None = None
    api_key: str | None = None
    cost: int = 1  # a search that fans out to ten shards can declare cost=10

    def __post_init__(self) -> None:
        if self.cost <= 0:
            raise ValidationError("cost must be positive")


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def with_headers(self, extra: Mapping[str, str]) -> Response:
        return replace(self, headers={**self.headers, **extra})


# --8<-- [end:decision]


# --8<-- [start:state]
@dataclass(slots=True)
class LimiterState:
    """Every field the five algorithms may keep for one key, in one record.

    A Redis-backed store keeps only what its algorithm touches - a token bucket
    is two numbers in a hash, a sliding log is a sorted set - but in process one
    record keeps the ``Storage`` protocol free of algorithm-specific methods.
    """

    updated_at: float
    level: float = 0.0  # token bucket: tokens left. leaky bucket: work queued
    window_start: float = 0.0
    count: int = 0  # requests inside the current window
    previous: int = 0  # sliding counter only: the previous window's final count
    log: deque[float] = field(default_factory=deque)  # sliding log only

    def idle_since(self, now: float) -> float:
        return now - self.updated_at


# --8<-- [end:state]
