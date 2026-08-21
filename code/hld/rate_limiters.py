"""Five rate-limiting algorithms behind one ``RateLimiter`` protocol, with an injected clock.

What the module demonstrates, in the order an interviewer asks about it:

* ``TokenBucket``: ``capacity`` tokens, refilled continuously at ``rate`` per second; a request
  spends ``cost`` tokens. Bursts up to the capacity are served at once, then the steady rate.
* ``LeakyBucket``: a queue of depth ``capacity`` drained at ``rate`` per second. It admits the
  same requests as a token bucket but *shapes* them: each admitted request carries the delay
  until it leaves the queue, so a burst exits at a constant rate instead of at once.
* ``FixedWindowCounter``: ``limit`` per aligned window; O(1) state, but a burst at the end of
  one window plus a burst at the start of the next lets 2x ``limit`` through in a moment.
* ``SlidingWindowLog``: timestamps of the accepted requests in the trailing window; exact, at
  O(limit) memory per key.
* ``SlidingWindowCounter``: the current window's count plus the previous window's count
  weighted by how much of it still overlaps the trailing window; O(1) state, approximate.

Every limiter answers ``allow(key, cost)`` with a ``Decision`` that carries what a 429 response
needs: ``limit``, ``remaining`` and ``retry_after`` (``Decision.headers`` renders the
``X-RateLimit-*`` and ``Retry-After`` headers). Keys are whatever identifies the caller: user
id, API key or client IP.

Public API reused by the rate-limiter case study: ``RateLimiter``, ``Decision``,
``TokenBucket``, ``LeakyBucket``, ``FixedWindowCounter``, ``SlidingWindowLog``,
``SlidingWindowCounter``. Each limiter has one ``threading.Lock`` that guards its per-key
table and every entry in it; the clock is read once per call, under that lock.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from common import Clock, FakeClock, SystemClock, ValidationError


# --8<-- [start:protocol]
@dataclass(frozen=True, slots=True)
class Decision:
    """The answer to one request, with everything the HTTP response needs."""

    allowed: bool
    limit: int  # the configured ceiling: bucket capacity or requests per window
    remaining: int  # permits left after this decision (floored)
    retry_after: float  # seconds until a request of this cost would pass; 0 when allowed
    delay: float = 0.0  # shaping limiters only: seconds this request waits before it is served

    def headers(self) -> dict[str, str]:
        out = {"X-RateLimit-Limit": str(self.limit), "X-RateLimit-Remaining": str(self.remaining)}
        if not self.allowed:
            out["Retry-After"] = str(max(1, math.ceil(self.retry_after)))
        return out


class RateLimiter(Protocol):
    """One decision per request; ``key`` identifies the caller (user, API key, IP)."""

    def allow(self, key: str, cost: int = 1) -> Decision: ...


def _check_cost(cost: int, ceiling: int) -> None:
    if cost <= 0:
        raise ValidationError("cost must be positive")
    if cost > ceiling:
        raise ValidationError(f"cost {cost} can never fit under the limit {ceiling}")


# --8<-- [end:protocol]


# --8<-- [start:buckets]
@dataclass(slots=True)
class _Bucket:
    level: float  # token bucket: tokens available; leaky bucket: work queued
    updated_at: float


class TokenBucket:
    """``capacity`` tokens refilled at ``rate`` per second; a request spends ``cost`` tokens.

    Refill is computed lazily from the elapsed time, so there is no timer thread and an idle
    key costs nothing. ``_lock`` guards ``_buckets`` and every bucket in it.
    """

    def __init__(self, rate: float, capacity: int, clock: Clock | None = None) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValidationError("rate and capacity must be positive")
        self._rate = rate
        self._capacity = capacity
        self._clock = clock or SystemClock()
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: int = 1) -> Decision:
        _check_cost(cost, self._capacity)
        with self._lock:
            now = self._clock.now()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._buckets[key] = _Bucket(float(self._capacity), now)
            bucket.level = min(self._capacity, bucket.level + (now - bucket.updated_at) * self._rate)
            bucket.updated_at = now
            if bucket.level >= cost:
                bucket.level -= cost
                return Decision(True, self._capacity, math.floor(bucket.level), 0.0)
            retry_after = (cost - bucket.level) / self._rate
            return Decision(False, self._capacity, math.floor(bucket.level), retry_after)


class LeakyBucket:
    """A queue of depth ``capacity`` that drains at ``rate`` per second (a traffic shaper).

    An admitted request is served once the work queued ahead of it has drained, so
    ``Decision.delay`` is ``queued / rate``: a burst of 5 at rate 5/s leaves at 0, 0.2, 0.4,
    0.6 and 0.8 s. Rejections happen only when the queue itself is full.
    """

    def __init__(self, rate: float, capacity: int, clock: Clock | None = None) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValidationError("rate and capacity must be positive")
        self._rate = rate
        self._capacity = capacity
        self._clock = clock or SystemClock()
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: int = 1) -> Decision:
        _check_cost(cost, self._capacity)
        with self._lock:
            now = self._clock.now()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._buckets[key] = _Bucket(0.0, now)
            bucket.level = max(0.0, bucket.level - (now - bucket.updated_at) * self._rate)
            bucket.updated_at = now
            if bucket.level + cost <= self._capacity:
                delay = bucket.level / self._rate
                bucket.level += cost
                remaining = math.floor(self._capacity - bucket.level)
                return Decision(True, self._capacity, remaining, 0.0, delay=delay)
            retry_after = (bucket.level + cost - self._capacity) / self._rate
            remaining = math.floor(self._capacity - bucket.level)
            return Decision(False, self._capacity, remaining, retry_after)


# --8<-- [end:buckets]


# --8<-- [start:windows]
@dataclass(slots=True)
class _Window:
    start: float
    count: int = 0
    previous: int = 0  # sliding window counter only: the last window's final count


class FixedWindowCounter:
    """``limit`` requests per aligned window of ``window`` seconds: a counter with a reset.

    The cheapest algorithm (one integer per key, a single ``INCR`` in Redis) and the leakiest:
    ``limit`` requests at the end of one window and ``limit`` more at the start of the next
    pass back to back.
    """

    def __init__(self, limit: int, window: float, clock: Clock | None = None) -> None:
        if limit <= 0 or window <= 0:
            raise ValidationError("limit and window must be positive")
        self._limit = limit
        self._window = window
        self._clock = clock or SystemClock()
        self._windows: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: int = 1) -> Decision:
        _check_cost(cost, self._limit)
        with self._lock:
            now = self._clock.now()
            start = math.floor(now / self._window) * self._window
            state = self._windows.get(key)
            if state is None or state.start != start:
                state = self._windows[key] = _Window(start)
            if state.count + cost <= self._limit:
                state.count += cost
                return Decision(True, self._limit, self._limit - state.count, 0.0)
            return Decision(False, self._limit, self._limit - state.count, start + self._window - now)


class SlidingWindowLog:
    """Exact: keep the timestamps of accepted requests in the trailing ``window`` seconds.

    Memory is O(limit) per key, which is why it suits low limits (login attempts, password
    resets) and not a public API at thousands of requests per minute per key.
    """

    def __init__(self, limit: int, window: float, clock: Clock | None = None) -> None:
        if limit <= 0 or window <= 0:
            raise ValidationError("limit and window must be positive")
        self._limit = limit
        self._window = window
        self._clock = clock or SystemClock()
        self._logs: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: int = 1) -> Decision:
        _check_cost(cost, self._limit)
        with self._lock:
            now = self._clock.now()
            log = self._logs.setdefault(key, deque())
            while log and log[0] <= now - self._window:
                log.popleft()
            if len(log) + cost <= self._limit:
                log.extend([now] * cost)
                return Decision(True, self._limit, self._limit - len(log), 0.0)
            # the request fits once enough of the oldest entries have aged out
            oldest_needed = log[len(log) + cost - self._limit - 1]
            return Decision(False, self._limit, self._limit - len(log), oldest_needed + self._window - now)


class SlidingWindowCounter:
    """Approximate: current count plus the previous window's count, weighted by its overlap.

    With 40% of the current window elapsed, 60% of the previous window still lies inside the
    trailing window, so ``estimate = previous * 0.6 + current``. Two integers per key, no
    boundary burst, and an error that assumes the previous window's requests were spread evenly.
    """

    def __init__(self, limit: int, window: float, clock: Clock | None = None) -> None:
        if limit <= 0 or window <= 0:
            raise ValidationError("limit and window must be positive")
        self._limit = limit
        self._window = window
        self._clock = clock or SystemClock()
        self._windows: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: int = 1) -> Decision:
        _check_cost(cost, self._limit)
        with self._lock:
            now = self._clock.now()
            start = math.floor(now / self._window) * self._window
            state = self._windows.get(key)
            if state is None:
                state = self._windows[key] = _Window(start)
            elif state.start != start:
                previous = state.count if state.start == start - self._window else 0
                state = self._windows[key] = _Window(start, previous=previous)
            weight = 1.0 - (now - start) / self._window
            estimate = state.previous * weight + state.count
            if estimate + cost <= self._limit:
                state.count += cost
                return Decision(True, self._limit, math.floor(self._limit - estimate - cost), 0.0)
            remaining = max(0, math.floor(self._limit - estimate))
            room = self._limit - state.count - cost  # what the previous window may still occupy
            if state.previous <= 0 or room < 0:
                retry_after = start + self._window - now  # only a fresh window can help
            else:
                retry_after = max(0.0, start + self._window * (1 - room / state.previous) - now)
            return Decision(False, self._limit, remaining, retry_after)


# --8<-- [end:windows]


def main() -> None:
    from concurrent.futures import ThreadPoolExecutor

    def limiters(clock: Clock) -> dict[str, RateLimiter]:
        return {
            "token bucket": TokenBucket(rate=5, capacity=5, clock=clock),
            "leaky bucket": LeakyBucket(rate=5, capacity=5, clock=clock),
            "fixed window": FixedWindowCounter(limit=5, window=1.0, clock=clock),
            "sliding log": SlidingWindowLog(limit=5, window=1.0, clock=clock),
            "sliding counter": SlidingWindowCounter(limit=5, window=1.0, clock=clock),
        }

    clock = FakeClock(start=0.0)
    print("limit 5 per second; 7 requests arrive at once at t=0:")
    for name, limiter in limiters(clock).items():
        decisions = [limiter.allow("alice") for _ in range(7)]
        allowed = sum(d.allowed for d in decisions)
        extra = ""
        if name == "leaky bucket":
            extra = "; served at t=" + ", ".join(f"{d.delay:.1f}" for d in decisions if d.allowed)
        print(f"  {name:<16}: {allowed} allowed, {7 - allowed} rejected, Retry-After {decisions[-1].retry_after:.1f} s{extra}")

    print("boundary burst: 5 requests at t=0.9, 5 at t=1.0, 5 at t=1.5 (allowed counts):")
    clock = FakeClock(start=0.0)
    for name, limiter in limiters(clock).items():
        counts = []
        for at in (0.9, 1.0, 1.5):
            clock.set(at)  # each limiter is fresh, so rewinding the shared clock is harmless
            counts.append(sum(limiter.allow("bob").allowed for _ in range(5)))
        print(f"  {name:<16}: t=0.9 -> {counts[0]}  t=1.0 -> {counts[1]}  t=1.5 -> {counts[2]}")

    clock = FakeClock(start=0.0)
    fixed = FixedWindowCounter(limit=5, window=60.0, clock=clock)
    for _ in range(5):
        fixed.allow("carol")
    clock.advance(12.5)
    print(f"429 headers after the 6th request in a minute: {fixed.allow('carol').headers()}")

    frozen = FakeClock(start=0.0)
    bucket = TokenBucket(rate=1, capacity=100, clock=frozen)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: bucket.allow("dave").allowed, range(800)))
    print(
        f"8 threads x 100 requests, capacity 100, frozen clock: "
        f"allowed={sum(results)} rejected={len(results) - sum(results)}"
    )


if __name__ == "__main__":
    main()
