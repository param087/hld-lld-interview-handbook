"""Cache stampede protection: single-flight loading and probabilistic early expiration.

What the module demonstrates, in the order an interviewer asks about it:

* ``CachedLoader.get`` is cache-aside with two guards. On a miss, the first caller for a key
  becomes the *leader* and runs the loader; concurrent callers for the same key wait for the
  leader's result instead of hitting the database (single-flight, also called request
  coalescing). On a hit, a caller may refresh the entry *early* with a probability that rises
  as expiry approaches (XFetch, Vattani, Chierichetti and Lowenstein 2015), so one request
  recomputes a hot key shortly before it would expire and nobody ever sees it expire.
* ``should_refresh_early`` is the XFetch test on its own: recompute when
  ``now - delta * beta * ln(u) >= expires_at``, ``u`` uniform in (0, 1], ``delta`` the
  measured recompute time, ``beta`` the aggressiveness (1.0 is the paper's default, 0 disables).
* ``LoaderStats`` counts hits, misses, loads, coalesced waits and early refreshes, so the
  demo can show 32 concurrent misses turning into one database read.

Public API reused by the distributed-cache case study: ``CachedLoader`` (``get``,
``invalidate``, ``stats``), ``LoaderStats``, ``should_refresh_early``. ``_lock`` guards the
in-flight table and the counters; the wrapped ``LRUCache`` has its own lock; the loader runs
outside both so a slow database never blocks other keys.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from common import Clock, FakeClock, SystemClock, ValidationError
from hld.lru_cache import LRUCache


# --8<-- [start:xfetch]
@dataclass(slots=True)
class _Entry[V]:
    value: V
    expires_at: float
    delta: float  # seconds the loader took to compute the value


@dataclass(frozen=True, slots=True)
class LoaderStats:
    hits: int  # served from a live entry without waiting
    misses: int  # found no live entry (whether they loaded or waited)
    loads: int  # loader invocations, including early refreshes
    coalesced: int  # misses that waited for another caller's load
    early_refreshes: int  # hits that chose to recompute before expiry


def should_refresh_early(
    now: float, expires_at: float, delta: float, beta: float, rng: random.Random
) -> bool:
    """XFetch: refresh with probability exp(-(expires_at - now) / (delta * beta)).

    Far from expiry the probability is negligible; within a few ``delta`` of expiry it climbs
    quickly and reaches 1 at expiry, so a popular key is recomputed by exactly one of the many
    requests just before it would go stale. ``beta`` above 1 refreshes earlier, below 1 later.
    """
    if beta <= 0 or delta <= 0:
        return False
    return now - delta * beta * math.log(1.0 - rng.random()) >= expires_at


# --8<-- [end:xfetch]


# --8<-- [start:loader]
class CachedLoader[K, V]:
    """Cache-aside reads through ``loader`` with single-flight and early-refresh protection.

    ``_inflight`` maps a key to the ``Event`` its leader will set when the load finishes;
    ``_lock`` guards it and the counters. Waiters block outside the lock, then re-read the
    cache; if the leader failed, the next waiter becomes the leader and retries, so one bad
    load never strands the callers behind it.
    """

    def __init__(
        self,
        loader: Callable[[K], V],
        ttl: float,
        *,
        capacity: int = 1024,
        beta: float = 1.0,
        coalesce: bool = True,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if ttl <= 0:
            raise ValidationError("ttl must be positive")
        if beta < 0:
            raise ValidationError("beta must be non-negative")
        self._loader = loader
        self._ttl = ttl
        self._beta = beta
        self._coalesce = coalesce
        self._clock = clock or SystemClock()
        self._rng = rng or random.Random()
        self._cache: LRUCache[K, _Entry[V]] = LRUCache(capacity, self._clock)
        self._inflight: dict[K, threading.Event] = {}
        self._lock = threading.Lock()
        self._hits = self._misses = self._loads = self._coalesced = self._early = 0

    @property
    def stats(self) -> LoaderStats:
        with self._lock:
            return LoaderStats(self._hits, self._misses, self._loads, self._coalesced, self._early)

    def invalidate(self, key: K) -> None:
        """Drop ``key`` so the next read loads it (the write path calls this after the DB write)."""
        self._cache.delete(key)

    def get(self, key: K) -> V:
        """The cached value, loading it on a miss: hit, early refresh, leader or waiter."""
        waited = False
        while True:
            entry = self._cache.get(key)
            if entry is not None:
                if waited:
                    return entry.value  # the leader's fresh value; counted as a coalesced miss
                return self._serve_hit(key, entry)
            with self._lock:
                if not waited:
                    self._misses += 1
                event = self._inflight.get(key) if self._coalesce else None
                if event is None:
                    event = threading.Event()
                    if self._coalesce:
                        self._inflight[key] = event
                    leader = True
                else:
                    self._coalesced += 1
                    leader = False
            if leader:
                return self._load(key, event)
            waited = True
            event.wait()  # the leader's load finished (or failed): re-read, maybe lead

    def _serve_hit(self, key: K, entry: _Entry[V]) -> V:
        refresh = should_refresh_early(
            self._clock.now(), entry.expires_at, entry.delta, self._beta, self._rng
        )
        with self._lock:
            self._hits += 1
            if not refresh or key in self._inflight:
                return entry.value  # fresh enough, or another caller is already refreshing
            self._early += 1
            event = self._inflight[key] = threading.Event()
        return self._load(key, event)

    def _load(self, key: K, event: threading.Event) -> V:
        with self._lock:
            self._loads += 1
        started = self._clock.now()
        try:
            value = self._loader(key)
            now = self._clock.now()
            entry = _Entry(value, now + self._ttl, max(now - started, 0.0))
            self._cache.put(key, entry, ttl=self._ttl)
            return value
        finally:
            with self._lock:
                if self._inflight.get(key) is event:
                    del self._inflight[key]
            event.set()


# --8<-- [end:loader]


def main() -> None:
    readers = 32
    barrier = threading.Barrier(readers)

    def stampeding_db(key: str) -> str:
        barrier.wait()  # returns only once every reader is inside the loader: the stampede
        return f"row for {key}"

    naive: CachedLoader[str, str] = CachedLoader(stampeding_db, ttl=60, coalesce=False, beta=0)
    with ThreadPoolExecutor(max_workers=readers) as pool:
        list(pool.map(naive.get, ["user:1"] * readers))
    print(f"{readers} concurrent readers, one cold key")
    print(f"  plain cache-aside : {naive.stats.loads} database loads, one per reader")

    def slow_db(key: str) -> str:
        time.sleep(0.02)
        return f"row for {key}"

    guarded: CachedLoader[str, str] = CachedLoader(slow_db, ttl=60, beta=0)
    with ThreadPoolExecutor(max_workers=readers) as pool:
        list(pool.map(guarded.get, ["user:1"] * readers))
    served = readers - guarded.stats.loads
    print(f"  single-flight     : {guarded.stats.loads} database load, {served} readers reused it")

    rng = random.Random(42)
    delta, beta, draws = 2.0, 1.0, 10_000
    print(f"XFetch early refresh, recompute delta={delta:.0f} s, beta={beta:.0f}, {draws:,} draws each")
    for remaining in (10.0, 5.0, 2.0, 1.0, 0.5):
        hits = sum(
            should_refresh_early(0.0, remaining, delta, beta, rng) for _ in range(draws)
        )
        theory = math.exp(-remaining / (delta * beta))
        print(
            f"  {remaining:>4} s before expiry: {hits / draws:>5.1%} of requests refresh "
            f"(theory {theory:.1%})"
        )

    def simulate(beta_value: float) -> LoaderStats:
        clock = FakeClock()

        def db(key: str) -> str:
            clock.advance(0.5)  # a 500 ms recompute
            return key

        loader: CachedLoader[str, str] = CachedLoader(
            db, ttl=10, beta=beta_value, clock=clock, rng=random.Random(7)
        )
        for _ in range(3_000):
            loader.get("feed:42")
            clock.advance(0.01)
        return loader.stats

    print("one hot key at 100 QPS for 30 s, ttl=10 s, recompute 0.5 s")
    plain, early = simulate(0.0), simulate(1.0)
    print(
        f"  single-flight only    : loads={plain.loads} misses={plain.misses} "
        f"(every expiry makes a reader wait 0.5 s)"
    )
    print(
        f"  + early expiration    : loads={early.loads} misses={early.misses} "
        f"early_refreshes={early.early_refreshes} (after warm-up nobody waits)"
    )


if __name__ == "__main__":
    main()
