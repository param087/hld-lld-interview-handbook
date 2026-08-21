"""The cache itself, the loading decorator, the sharded variant and the TTL sweeper.

All the locking in this package lives in this file.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from dataclasses import replace

from common import Clock, SystemClock, ValidationError
from lld.in_memory_cache.models import (
    CacheStats,
    CapacityError,
    Entry,
    EvictionPolicyName,
    KeyMissingError,
)
from lld.in_memory_cache.policies import EvictionPolicy, LRUPolicy, make_policy

DEFAULT_SHARDS = 8
DEFAULT_LOAD_STRIPES = 64


# --8<-- [start:cache]
class Cache[K: Hashable, V]:
    """Bounded, thread-safe, O(1) cache: a dict for the values, a policy for the order.

    One ``threading.RLock`` guards three pieces of state that must always agree:
    ``_entries``, the policy's ordering structure and the counters. Splitting them
    would let a reader see a key the policy has already evicted. The lock is
    *reentrant* so a public method may call another one (``purge_expired`` walks
    keys and calls the same removal helper as ``delete``) without a second lock
    protocol; an uncontended acquire costs about 17 ns, so the reentrancy is not
    what your latency budget will notice.
    """

    def __init__(
        self,
        capacity: int,
        policy: EvictionPolicy | None = None,
        clock: Clock | None = None,
        default_ttl: float | None = None,
    ) -> None:
        if capacity < 1:
            raise CapacityError(f"capacity must be at least 1, got {capacity}")
        if default_ttl is not None and default_ttl <= 0:
            raise ValidationError("default_ttl must be positive")
        self.capacity = capacity
        # `policy or LRUPolicy()` would be a bug: an empty policy is falsy because
        # it defines __len__, so every caller would silently get an LRU cache.
        self._policy = LRUPolicy() if policy is None else policy
        self._clock = clock or SystemClock()
        self._default_ttl = default_ttl
        self._entries: dict[K, Entry[V]] = {}
        self._lock = threading.RLock()
        self._hits = self._misses = self._evictions = self._expirations = 0

    def try_get(self, key: K) -> Entry[V] | None:
        """The full read path: expiry check, recency update, hit or miss counted.

        It returns the *entry*, not the value, so a caller can tell "no such key"
        from "a key whose value is ``None``" - which is what makes caching a
        negative lookup possible.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired(self._clock.now()):
                self._discard(key)  # lazy expiry: the reader pays for the cleanup
                self._expirations += 1
                self._misses += 1
                return None
            self._policy.on_access(key)
            self._hits += 1
            return entry

    def get(self, key: K, default: V | None = None) -> V | None:
        entry = self.try_get(key)
        return default if entry is None else entry.value

    def __getitem__(self, key: K) -> V:
        entry = self.try_get(key)
        if entry is None:
            raise KeyMissingError(f"{key!r} is not in the cache")
        return entry.value

    def put(self, key: K, value: V, ttl: float | None = None) -> None:
        """Insert or overwrite. Eviction happens only on the insert path."""
        if ttl is not None and ttl <= 0:
            raise ValidationError("ttl must be positive; use delete() to remove a key")
        deadline = self._deadline(self._default_ttl if ttl is None else ttl)
        with self._lock:
            if key in self._entries:
                self._entries[key] = Entry(value, deadline)
                self._policy.on_access(key)  # a rewrite counts as a use
                return
            if len(self._entries) >= self.capacity:
                self._evict_one()
            self._entries[key] = Entry(value, deadline)
            self._policy.on_insert(key)

    def delete(self, key: K) -> bool:
        with self._lock:
            if key not in self._entries:
                return False
            self._discard(key)
            return True

    def __contains__(self, key: K) -> bool:
        """Membership without touching recency or the counters; expired keys are absent."""
        with self._lock:
            entry = self._entries.get(key)
            return entry is not None and not entry.is_expired(self._clock.now())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def purge_expired(self) -> int:
        """Active expiry: drop every entry past its deadline. O(n), so call it on a timer."""
        with self._lock:
            now = self._clock.now()
            dead = [k for k, entry in self._entries.items() if entry.is_expired(now)]
            for key in dead:
                self._discard(key)
            self._expirations += len(dead)
            return len(dead)

    def keys(self) -> list[K]:
        """A snapshot of the live keys; expired entries are left out."""
        with self._lock:
            now = self._clock.now()
            return [key for key, entry in self._entries.items() if not entry.is_expired(now)]

    def eviction_order(self) -> list[Hashable]:
        """Tracked keys, victim first - the cheapest way to assert a policy in a test."""
        with self._lock:
            return self._policy.keys()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._policy.clear()

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                expirations=self._expirations,
                size=len(self._entries),
                capacity=self.capacity,
            )

    def _deadline(self, ttl: float | None) -> float | None:
        return None if ttl is None else self._clock.now() + ttl

    def _evict_one(self) -> None:
        """Caller holds the lock. The policy picks; the cache forgets the value."""
        victim = self._policy.evict()
        del self._entries[victim]
        self._evictions += 1

    def _discard(self, key: K) -> None:
        """Caller holds the lock. Remove a key from both structures at once."""
        del self._entries[key]
        self._policy.on_remove(key)


# --8<-- [end:cache]


# --8<-- [start:loading]
class LoadingCache[K: Hashable, V]:
    """Cache-aside behind one call, with a single-flight guard against stampedes.

    Decorator: it wraps *a* ``Cache`` and adds exactly one behaviour. On a miss,
    one thread runs the loader while every other thread asking for the same key
    waits on the same stripe lock and then finds the value already stored. Without
    the guard, a hot key expiring under 500 requests/s sends 500 identical queries
    to the database in the time it takes to answer one.

    The guard is a fixed array of locks chosen by ``hash(key)``, not a dict of
    locks per key: no bookkeeping, no leak, and the only cost is that two keys
    that share a stripe load one after the other instead of together.
    """

    def __init__(
        self,
        inner: Cache[K, V],
        loader: Callable[[K], V],
        ttl: float | None = None,
        stripes: int = DEFAULT_LOAD_STRIPES,
    ) -> None:
        if stripes < 1:
            raise ValidationError("stripes must be at least 1")
        self._inner = inner
        self._loader = loader
        self._ttl = ttl
        self._stripes = [threading.Lock() for _ in range(stripes)]
        self._counter_lock = threading.Lock()
        self._loads = self._coalesced = 0

    def get(self, key: K) -> V:
        entry = self._inner.try_get(key)
        if entry is not None:
            return entry.value
        with self._stripes[hash(key) % len(self._stripes)]:
            entry = self._inner.try_get(key)  # a waiter re-checks: the winner may have filled it
            if entry is not None:
                self._bump(coalesced=1)
                return entry.value
            value = self._loader(key)  # a loader failure propagates and is not cached
            self._inner.put(key, value, ttl=self._ttl)
            self._bump(loads=1)
            return value

    def invalidate(self, key: K) -> bool:
        return self._inner.delete(key)

    def stats(self) -> CacheStats:
        with self._counter_lock:
            return replace(self._inner.stats(), loads=self._loads, coalesced=self._coalesced)

    def _bump(self, loads: int = 0, coalesced: int = 0) -> None:
        with self._counter_lock:
            self._loads += loads
            self._coalesced += coalesced


# --8<-- [end:loading]


# --8<-- [start:sharded]
class ShardedCache[K: Hashable, V]:
    """N independent caches picked by ``hash(key)`` - the answer to "one lock is a bottleneck".

    You cannot stripe a single strict LRU, because the recency list is global
    state that every read mutates. What production caches do instead is exactly
    this: shard the key space, give each shard its own lock and its own order, and
    accept an eviction order that is per shard rather than global. Contention
    falls roughly with the shard count; the hit ratio moves by a fraction of a
    percent because the shards get statistically similar load.
    """

    def __init__(
        self,
        capacity: int,
        shards: int = DEFAULT_SHARDS,
        policy: EvictionPolicyName | str = EvictionPolicyName.LRU,
        clock: Clock | None = None,
        default_ttl: float | None = None,
    ) -> None:
        if shards < 1:
            raise ValidationError("shards must be at least 1")
        if capacity < shards:
            raise CapacityError(f"capacity {capacity} cannot be split across {shards} shards")
        per_shard = capacity // shards
        self._shards: list[Cache[K, V]] = [
            Cache(per_shard, make_policy(policy), clock=clock, default_ttl=default_ttl)
            for _ in range(shards)
        ]

    def shard_for(self, key: K) -> Cache[K, V]:
        return self._shards[hash(key) % len(self._shards)]

    def get(self, key: K, default: V | None = None) -> V | None:
        return self.shard_for(key).get(key, default)

    def put(self, key: K, value: V, ttl: float | None = None) -> None:
        self.shard_for(key).put(key, value, ttl=ttl)

    def delete(self, key: K) -> bool:
        return self.shard_for(key).delete(key)

    def __contains__(self, key: K) -> bool:
        return key in self.shard_for(key)

    def __len__(self) -> int:
        return sum(len(shard) for shard in self._shards)

    def stats(self) -> CacheStats:
        parts = [shard.stats() for shard in self._shards]
        return CacheStats(
            hits=sum(p.hits for p in parts),
            misses=sum(p.misses for p in parts),
            evictions=sum(p.evictions for p in parts),
            expirations=sum(p.expirations for p in parts),
            size=sum(p.size for p in parts),
            capacity=sum(p.capacity for p in parts),
        )


# --8<-- [end:sharded]


# --8<-- [start:sweeper]
class TtlSweeper[K: Hashable, V]:
    """Optional background thread that calls ``purge_expired`` every ``interval`` seconds.

    Lazy expiry alone is already correct - an expired entry can never be read.
    The sweeper is about *memory*: a key written once, expired and never read
    again would otherwise hold its value until eviction pressure removed it.

    Shutdown is the part interviewers probe. The loop waits on an ``Event``
    instead of sleeping, so ``stop()`` returns as soon as the flag is set rather
    than after the rest of an interval, and the thread is a daemon so a forgotten
    ``stop()`` cannot keep the process alive.
    """

    def __init__(self, cache: Cache[K, V], interval: float, name: str = "ttl-sweeper") -> None:
        if interval <= 0:
            raise ValidationError("interval must be positive")
        self._cache = cache
        self._interval = interval
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._purged = 0

    @property
    def purged(self) -> int:
        """Entries reclaimed so far. Written by the sweeper thread only."""
        return self._purged

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stopping.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stopping.wait(self._interval):
            self._purged += self._cache.purge_expired()


# --8<-- [end:sweeper]
