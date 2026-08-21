"""The five algorithms, each a Strategy behind one ``allow`` method.

None of them owns its state. They mutate one record inside a ``Storage``, which
is the seam that lets the same class run against a process-local dict today and
a Redis Lua script tomorrow.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Protocol

from common import Clock, SystemClock, ValidationError
from lld.rate_limiter.models import Decision, LimiterState

# What a limiter hands the storage: given the current record (or None the first
# time this key is seen), return the record to keep and the answer to the caller.
type Mutation = Callable[[LimiterState | None], tuple[LimiterState, Decision]]


# --8<-- [start:protocols]
class Storage(Protocol):
    """Where limiter state lives, with one hard contract: ``apply`` is atomic per key.

    Read, decide and write must be indivisible. In process that is a lock; in
    Redis it is a Lua script or a ``WATCH``/``MULTI`` loop - which is why the
    distributed version ships the algorithm to the data instead of pulling the
    data to the algorithm. A storage that offered plain ``get`` and ``set`` would
    make every limiter a read-modify-write race.
    """

    def apply(self, key: str, now: float, mutate: Mutation) -> Decision: ...

    def evict_idle(self, cutoff: float) -> int:
        """Forget keys untouched since ``cutoff``. Returns how many were dropped."""

    def __len__(self) -> int: ...


class RateLimiter(Protocol):
    """One decision per request. ``key`` already identifies rule plus caller."""

    def allow(self, key: str, cost: int = 1) -> Decision: ...


# --8<-- [end:protocols]


# --8<-- [start:template]
class StorageBackedLimiter(ABC):
    """Template Method: read the clock once, then mutate exactly one key atomically.

    Every algorithm answers the same two questions and nothing else: what does a
    key look like the first time it is seen, and what does one request do to it.
    Validation, the clock read and the storage round trip are written once here.
    """

    def __init__(
        self,
        capacity: int,
        window_seconds: float,
        storage: Storage,
        clock: Clock | None = None,
        rule: str = "",
    ) -> None:
        if capacity <= 0 or window_seconds <= 0:
            raise ValidationError("capacity and window must be positive")
        self._capacity = capacity
        self._window = window_seconds
        self._storage = storage
        self._clock = clock or SystemClock()
        self._rule = rule

    @property
    def capacity(self) -> int:
        return self._capacity

    def allow(self, key: str, cost: int = 1) -> Decision:
        if cost <= 0:
            raise ValidationError("cost must be positive")
        if cost > self._capacity:
            raise ValidationError(f"cost {cost} can never fit under a limit of {self._capacity}")
        now = self._clock.now()

        def mutate(state: LimiterState | None) -> tuple[LimiterState, Decision]:
            record = self._initial(now) if state is None else state
            return record, self._decide(record, now, cost)

        return self._storage.apply(self._namespaced(key), now, mutate)

    def _namespaced(self, key: str) -> str:
        """Every rule owns its own key space; two rules can never share one counter."""
        return f"{self._rule}|{key}"

    @abstractmethod
    def _initial(self, now: float) -> LimiterState:
        """The record for a key nobody has asked about yet."""

    @abstractmethod
    def _decide(self, state: LimiterState, now: float, cost: int) -> Decision:
        """Mutate the record for one request and answer."""

    def _verdict(self, allowed: bool, remaining: float, retry_after: float = 0.0, delay: float = 0.0) -> Decision:
        return Decision(
            allowed=allowed,
            limit=self._capacity,
            remaining=max(0, math.floor(remaining)),
            retry_after=max(0.0, retry_after),
            rule=self._rule,
            delay=delay,
        )


# --8<-- [end:template]


# --8<-- [start:buckets]
class TokenBucket(StorageBackedLimiter):
    """``capacity`` tokens refilled continuously at ``capacity / window`` per second.

    The default choice for a public API: it absorbs a burst up to the capacity and
    then settles to the sustained rate, and an idle key costs nothing because the
    refill is computed from the elapsed time rather than by a timer.
    """

    def _initial(self, now: float) -> LimiterState:
        return LimiterState(updated_at=now, level=float(self._capacity))

    def _decide(self, state: LimiterState, now: float, cost: int) -> Decision:
        rate = self._capacity / self._window
        state.level = min(float(self._capacity), state.level + (now - state.updated_at) * rate)
        if state.level >= cost:
            state.level -= cost
            return self._verdict(True, state.level)
        return self._verdict(False, state.level, retry_after=(cost - state.level) / rate)


class LeakyBucket(StorageBackedLimiter):
    """A queue of depth ``capacity`` draining at a constant rate: a shaper, not a gate.

    It admits the same requests a token bucket would, but each admitted request
    carries the delay until the work queued ahead of it has drained, so a burst
    leaves at an even rate instead of all at once. Use it in front of something
    that cannot absorb a spike at all, such as a payment provider.
    """

    def _initial(self, now: float) -> LimiterState:
        return LimiterState(updated_at=now, level=0.0)

    def _decide(self, state: LimiterState, now: float, cost: int) -> Decision:
        rate = self._capacity / self._window
        state.level = max(0.0, state.level - (now - state.updated_at) * rate)
        room = self._capacity - state.level
        if cost <= room:
            delay = state.level / rate
            state.level += cost
            return self._verdict(True, self._capacity - state.level, delay=delay)
        return self._verdict(False, room, retry_after=(cost - room) / rate)


# --8<-- [end:buckets]


# --8<-- [start:windows]
class FixedWindowCounter(StorageBackedLimiter):
    """One counter per aligned window. The cheapest algorithm and the leakiest.

    ``limit`` requests just before a boundary plus ``limit`` just after pass back
    to back, so a limit of 100 per minute can serve 200 in two seconds. Say that
    trade out loud before the interviewer does.
    """

    def _initial(self, now: float) -> LimiterState:
        return LimiterState(updated_at=now, window_start=self._align(now))

    def _decide(self, state: LimiterState, now: float, cost: int) -> Decision:
        start = self._align(now)
        if state.window_start != start:
            state.window_start, state.count = start, 0
        if state.count + cost <= self._capacity:
            state.count += cost
            return self._verdict(True, self._capacity - state.count)
        return self._verdict(False, self._capacity - state.count, retry_after=start + self._window - now)

    def _align(self, now: float) -> float:
        return math.floor(now / self._window) * self._window


class SlidingWindowLog(StorageBackedLimiter):
    """Exact: the timestamps of the accepted requests in the trailing window.

    Memory is O(limit) per key, which makes it right for small limits on
    expensive actions - login attempts, password resets, one-time-code sends -
    and wrong for a public API at thousands of requests per key per minute.
    """

    def _initial(self, now: float) -> LimiterState:
        return LimiterState(updated_at=now)

    def _decide(self, state: LimiterState, now: float, cost: int) -> Decision:
        horizon = now - self._window
        while state.log and state.log[0] <= horizon:
            state.log.popleft()
        if len(state.log) + cost <= self._capacity:
            state.log.extend([now] * cost)
            return self._verdict(True, self._capacity - len(state.log))
        # the request fits once this many of the oldest entries have aged out
        index = len(state.log) + cost - self._capacity - 1
        return self._verdict(False, 0, retry_after=state.log[index] + self._window - now)


class SlidingWindowCounter(StorageBackedLimiter):
    """Approximate: this window's count plus the previous one's, weighted by overlap.

    Two integers per key, no boundary burst, and one assumption - that the
    previous window's requests were spread evenly across it. That assumption is
    wrong for a single spike and close enough for aggregate traffic, which is why
    this is what most gateways actually run.
    """

    def _initial(self, now: float) -> LimiterState:
        return LimiterState(updated_at=now, window_start=self._align(now))

    def _decide(self, state: LimiterState, now: float, cost: int) -> Decision:
        start = self._align(now)
        if state.window_start != start:
            carried = state.count if state.window_start == start - self._window else 0
            state.window_start, state.previous, state.count = start, carried, 0
        overlap = 1.0 - (now - start) / self._window
        estimate = state.previous * overlap + state.count
        if estimate + cost <= self._capacity:
            state.count += cost
            return self._verdict(True, self._capacity - estimate - cost)
        return self._verdict(False, self._capacity - estimate, retry_after=self._retry_after(state, now, start, cost))

    def _retry_after(self, state: LimiterState, now: float, start: float, cost: int) -> float:
        room = self._capacity - state.count - cost  # what the previous window may still occupy
        if state.previous <= 0 or room < 0:
            return start + self._window - now  # only a fresh window can help
        return start + self._window * (1 - room / state.previous) - now

    def _align(self, now: float) -> float:
        return math.floor(now / self._window) * self._window


# --8<-- [end:windows]
