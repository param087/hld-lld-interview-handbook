"""A distributed rate limiter: a sliding-window counter over a simulated Redis.

``hld.rate_limiters`` compares the five algorithms in one process. This module answers the
question that follows in the interview: *what changes when the counter lives on another
machine and fifty gateway nodes share it?* Four pieces, in the order an interviewer asks:

* ``FakeRedis`` stands in for one Redis node. Its lock plays the part of the single-threaded
  event loop: ``eval`` runs a whole script while holding it (atomic, like Lua), while ``mget``
  and ``incr`` are separate round trips (not atomic, and that is the point).
* ``RedisSlidingWindow`` keeps two counters per key -- the current window and the previous one
  -- and estimates the trailing window as ``previous * overlap + current``. ``allow`` does it
  in one atomic script; ``allow_racy`` does the same arithmetic as read-then-write, which is
  the bug the interviewer is fishing for.
* ``TwoTierLimiter`` reserves permits from the global counter in chunks and serves them from
  memory, cutting round trips by a factor of ``chunk`` at the cost of a slightly conservative
  limit.
* ``RuleSet`` holds the configuration and swaps it atomically, so a hot reload can never be
  observed half-applied.

``Decision`` and the ``RateLimiter`` protocol are imported from ``hld.rate_limiters`` so every
limiter in the handbook returns the same 429 payload.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeVar

from common import Clock, SystemClock, ValidationError
from hld.rate_limiters import Decision, RateLimiter

T = TypeVar("T")


def _check_cost(cost: int, ceiling: int) -> None:
    if cost <= 0:
        raise ValidationError("cost must be positive")
    if cost > ceiling:
        raise ValidationError(f"cost {cost} can never fit under the limit {ceiling}")


# --8<-- [start:redis]
@dataclass(slots=True)
class _Entry:
    value: float
    expires_at: float


class ScriptContext:
    """What a Lua script sees: reads and writes that all happen inside one atomic execution."""

    def __init__(self, redis: FakeRedis, now: float) -> None:
        self._redis = redis
        self.now = now

    def get(self, key: str) -> float:
        return self._redis.read_unlocked(key, self.now)

    def incr(self, key: str, amount: float, ttl: float) -> float:
        return self._redis.incr_unlocked(key, amount, ttl, self.now)


class FakeRedis:
    """One Redis node. ``_lock`` stands for its single-threaded event loop.

    ``eval`` holds the lock for the whole script, which is what makes check-then-increment
    atomic in production. ``mget`` and ``incr`` are separate commands, so two clients can
    interleave between them -- exactly the race in :meth:`RedisSlidingWindow.allow_racy`.

    ``after_command`` is a test seam fired *outside* the lock with the command name, so a test
    can pin one interleaving with a ``threading.Barrier`` instead of hoping for a race.
    """

    def __init__(
        self,
        clock: Clock | None = None,
        after_command: Callable[[str], None] | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._after_command = after_command
        self._data: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._commands = 0

    @property
    def commands(self) -> int:
        """Round trips served so far: the number this whole design exists to reduce."""
        with self._lock:
            return self._commands

    # -- unlocked internals: the caller already holds _lock --------------------------------
    def read_unlocked(self, key: str, now: float) -> float:
        entry = self._data.get(key)
        return 0.0 if entry is None or entry.expires_at <= now else entry.value

    def incr_unlocked(self, key: str, amount: float, ttl: float, now: float) -> float:
        entry = self._data.get(key)
        if entry is None or entry.expires_at <= now:
            entry = self._data[key] = _Entry(0.0, now + ttl)
        entry.value += amount
        entry.expires_at = max(entry.expires_at, now + ttl)
        return entry.value

    # -- commands ---------------------------------------------------------------------------
    def mget(self, keys: Sequence[str]) -> list[float]:
        with self._lock:
            self._commands += 1
            now = self._clock.now()
            values = [self.read_unlocked(key, now) for key in keys]
        self._fire("mget")
        return values

    def incr(self, key: str, amount: float = 1.0, ttl: float = 60.0) -> float:
        with self._lock:
            self._commands += 1
            value = self.incr_unlocked(key, amount, ttl, self._clock.now())
        self._fire("incr")
        return value

    def eval(self, script: Callable[[ScriptContext], T]) -> T:
        """One round trip, one atomic execution: the Lua script of the real design."""
        with self._lock:
            self._commands += 1
            result = script(ScriptContext(self, self._clock.now()))
        self._fire("eval")
        return result

    def _fire(self, command: str) -> None:
        if self._after_command is not None:
            self._after_command(command)


# --8<-- [end:redis]


# --8<-- [start:sliding_window]
@dataclass(frozen=True, slots=True)
class Rule:
    """``limit`` permits per ``window`` seconds for every key in ``scope``."""

    scope: str
    limit: int
    window: float

    def __post_init__(self) -> None:
        if self.limit <= 0 or self.window <= 0:
            raise ValidationError("limit and window must be positive")


class RedisSlidingWindow:
    """Sliding-window counter in Redis: two counters per key, one round trip per decision.

    The trailing window is estimated as ``previous * overlap + current``, where ``overlap`` is
    the fraction of the previous window that still lies inside it. Two integers per key instead
    of a timestamp log, no boundary burst, and an error that only shows up when the previous
    window's traffic was very unevenly spread.
    """

    def __init__(self, redis: FakeRedis, rule: Rule, clock: Clock | None = None) -> None:
        self._redis = redis
        self._rule = rule
        self._clock = clock or SystemClock()

    @property
    def rule(self) -> Rule:
        return self._rule

    def allow(self, key: str, cost: int = 1) -> Decision:
        """Read both counters, decide and increment inside one atomic script."""
        _check_cost(cost, self._rule.limit)
        return self._redis.eval(lambda ctx: self._script(ctx, key, cost))

    def allow_racy(self, key: str, cost: int = 1) -> Decision:
        """The same arithmetic split into read-then-write: the bug interviewers ask about.

        Every gateway that reads the counters before any of them writes sees the same room and
        admits, so the limit is exceeded by however many nodes raced.
        """
        _check_cost(cost, self._rule.limit)
        now = self._clock.now()
        start = self._window_start(now)
        current_key, previous_key = self._keys(key, start)
        current, previous = self._redis.mget([current_key, previous_key])
        estimate = self._estimate(current, previous, now, start)
        if estimate + cost > self._rule.limit:
            return self._reject(current, previous, now, start, cost)
        self._redis.incr(current_key, cost, ttl=2 * self._rule.window)
        return self._admit(estimate, cost)

    # -- the script body ---------------------------------------------------------------------
    def _script(self, ctx: ScriptContext, key: str, cost: int) -> Decision:
        start = self._window_start(ctx.now)
        current_key, previous_key = self._keys(key, start)
        current, previous = ctx.get(current_key), ctx.get(previous_key)
        estimate = self._estimate(current, previous, ctx.now, start)
        if estimate + cost > self._rule.limit:
            return self._reject(current, previous, ctx.now, start, cost)
        ctx.incr(current_key, cost, ttl=2 * self._rule.window)
        return self._admit(estimate, cost)

    def _window_start(self, now: float) -> float:
        return math.floor(now / self._rule.window) * self._rule.window

    def _keys(self, key: str, start: float) -> tuple[str, str]:
        window = self._rule.window
        bucket = int(start / window)
        return f"{self._rule.scope}:{key}:{bucket}", f"{self._rule.scope}:{key}:{bucket - 1}"

    def _estimate(self, current: float, previous: float, now: float, start: float) -> float:
        overlap = 1.0 - (now - start) / self._rule.window
        return previous * overlap + current

    def _admit(self, estimate: float, cost: int) -> Decision:
        remaining = math.floor(self._rule.limit - estimate - cost)
        return Decision(True, self._rule.limit, max(0, remaining), 0.0)

    def _reject(
        self, current: float, previous: float, now: float, start: float, cost: int
    ) -> Decision:
        rule = self._rule
        remaining = max(0, math.floor(rule.limit - self._estimate(current, previous, now, start)))
        room = rule.limit - current - cost  # what the previous window may still occupy
        if previous <= 0 or room < 0:
            retry_after = start + rule.window - now  # only a fresh window can help
        else:
            retry_after = max(0.0, start + rule.window * (1 - room / previous) - now)
        return Decision(False, rule.limit, remaining, retry_after)


# --8<-- [end:sliding_window]


# --8<-- [start:two_tier]
class TwoTierLimiter:
    """Local budget first, global counter second: the pattern that keeps Redis off the hot path.

    A node reserves ``chunk`` permits from the global limiter in one call, then serves ``chunk``
    requests from memory. Round trips drop by a factor of ``chunk``. The cost is accuracy in one
    direction only: a reservation the node never spends is lost when the window rolls over, so
    the effective limit is slightly *under* the configured one -- never over it.

    Once the global counter refuses, the node stops asking until ``retry_after`` elapses.
    Without that local cooldown a limiter under attack sends *more* traffic to Redis than an
    unthrottled service does, which is how a rate limiter becomes the outage.

    ``_lock`` guards ``_reserved``, ``_expires_at`` and ``_blocked_until``, and is always taken
    before the Redis lock, so the two can never deadlock.
    """

    def __init__(
        self,
        limiter: RateLimiter,
        rule: Rule,
        chunk: int = 20,
        clock: Clock | None = None,
    ) -> None:
        if not 1 <= chunk <= rule.limit:
            raise ValidationError(f"chunk must be in 1..{rule.limit}")
        self._limiter = limiter
        self._rule = rule
        self._chunk = chunk
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._reserved = 0
        self._expires_at = 0.0
        self._blocked_until = 0.0
        self._round_trips = 0

    @property
    def round_trips(self) -> int:
        with self._lock:
            return self._round_trips

    @property
    def reserved(self) -> int:
        with self._lock:
            return self._reserved

    def allow(self, key: str, cost: int = 1) -> Decision:
        _check_cost(cost, self._rule.limit)
        with self._lock:
            now = self._clock.now()
            if now >= self._expires_at:
                self._reserved = 0  # a reservation never outlives the window it came from
            if self._reserved >= cost:
                self._reserved -= cost
                return Decision(True, self._rule.limit, self._reserved, 0.0)
            if now < self._blocked_until:
                return Decision(False, self._rule.limit, 0, self._blocked_until - now)
            want = max(cost, self._chunk)
            self._round_trips += 1
            if self._limiter.allow(key, want).allowed:
                self._reserved = want - cost
                self._expires_at = now + self._rule.window
                return Decision(True, self._rule.limit, self._reserved, 0.0)
            # near the end of the budget a chunk no longer fits: ask for exactly what is needed
            self._round_trips += 1
            exact = self._limiter.allow(key, cost)
            if not exact.allowed:
                self._blocked_until = now + min(exact.retry_after, self._rule.window)
            return exact


# --8<-- [end:two_tier]


# --8<-- [start:rules]
class RuleSet:
    """Rules resolved per scope, replaced atomically on reload.

    Readers take no lock: they read one attribute that always points at a complete, immutable
    mapping. A reload builds the next mapping and rebinds that attribute, so a request in flight
    sees either the whole old configuration or the whole new one, never a half-applied one.
    ``_write_lock`` serialises reloads with each other, not with readers.
    """

    def __init__(self, rules: Iterable[Rule], default: Rule) -> None:
        self._default = default
        self._rules = self._index(rules)
        self._version = 1
        self._write_lock = threading.Lock()

    @staticmethod
    def _index(rules: Iterable[Rule]) -> Mapping[str, Rule]:
        return MappingProxyType({rule.scope: rule for rule in rules})

    @property
    def version(self) -> int:
        return self._version

    def resolve(self, scope: str) -> Rule:
        return self._rules.get(scope, self._default)

    def reload(self, rules: Iterable[Rule]) -> None:
        snapshot = self._index(rules)  # build first, publish second
        with self._write_lock:
            self._rules = snapshot
            self._version += 1


# --8<-- [end:rules]


def main() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from common import FakeClock

    rule = Rule(scope="free", limit=5, window=1.0)

    clock = FakeClock(start=0.0)
    limiter = RedisSlidingWindow(FakeRedis(clock=clock), rule, clock=clock)
    decisions = [limiter.allow("user:ann") for _ in range(7)]
    allowed = sum(decision.allowed for decision in decisions)
    print(f"limit 5/s, 7 requests at t=0: {allowed} allowed, {7 - allowed} rejected")
    print(f"  429 payload: {decisions[-1].headers()}")

    clock = FakeClock(start=0.5)
    edge = RedisSlidingWindow(FakeRedis(clock=clock), rule, clock=clock)
    counts = []
    for at in (0.5, 1.0, 1.5, 2.0):
        clock.set(at)
        counts.append(sum(edge.allow("user:bob").allowed for _ in range(5)))
    print(
        "boundary burst, 5 requests at t=0.5/1.0/1.5/2.0: "
        + " ".join(f"{at}->{n}" for at, n in zip((0.5, 1.0, 1.5, 2.0), counts, strict=True))
        + "  (a fixed window would pass 5 at t=1.0)"
    )

    barrier = threading.Barrier(8)

    def hold_after_read(command: str) -> None:
        if command == "mget":
            barrier.wait(timeout=5.0)  # every gateway reads before any of them writes

    race_clock = FakeClock(start=0.0)
    racy = RedisSlidingWindow(
        FakeRedis(clock=race_clock, after_command=hold_after_read), rule, clock=race_clock
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        racy_allowed = sum(pool.map(lambda _: racy.allow_racy("user:cat").allowed, range(8)))
    print(f"8 gateways, read-then-write: {racy_allowed} allowed for a limit of 5 -- the race")

    atomic_clock = FakeClock(start=0.0)
    atomic = RedisSlidingWindow(FakeRedis(clock=atomic_clock), rule, clock=atomic_clock)
    with ThreadPoolExecutor(max_workers=8) as pool:
        atomic_allowed = sum(pool.map(lambda _: atomic.allow("user:cat").allowed, range(8)))
    print(f"8 gateways, one atomic script: {atomic_allowed} allowed -- exactly the limit")

    plan = Rule(scope="pro", limit=10_000, window=60.0)
    tier_clock = FakeClock(start=0.0)
    redis = FakeRedis(clock=tier_clock)
    node = TwoTierLimiter(
        RedisSlidingWindow(redis, plan, clock=tier_clock), plan, chunk=50, clock=tier_clock
    )
    served = sum(node.allow("tenant:acme").allowed for _ in range(2_000))
    print(
        f"two-tier, chunk 50: {served}/2000 allowed using {node.round_trips} Redis round trips "
        f"({redis.commands} commands) instead of 2000"
    )

    rules = RuleSet([Rule("free", 5, 1.0), Rule("pro", 1_000, 1.0)], default=Rule("anon", 60, 60.0))
    print(f"rules v{rules.version}: free={rules.resolve('free').limit}/s, "
          f"unknown scope falls back to {rules.resolve('nobody').limit}/min")
    rules.reload([Rule("free", 20, 1.0), Rule("pro", 1_000, 1.0)])
    print(f"hot reload -> v{rules.version}: free={rules.resolve('free').limit}/s, "
          f"no restart and no lock on the read path")


if __name__ == "__main__":
    main()
