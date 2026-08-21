"""O(1) LRU cache with per-entry TTL, plus a hit-ratio simulator for cache-sizing arguments.

What the module demonstrates, in the order an interviewer asks about it:

* ``LRUCache`` pairs a dict (O(1) lookup) with a circular doubly linked list (O(1) recency
  update): every ``get`` and ``put`` moves the entry to the front, every eviction drops the
  entry at the back, the least recently used one.
* Entries carry an optional TTL that is checked lazily with the injected ``Clock``: an
  expired entry is a miss and is dropped when touched (Redis-style lazy expiry), so no
  background sweeper is needed for correctness.
* ``CacheStats`` counts hits, misses, evictions and expirations, so the hit ratio that every
  capacity discussion hinges on is measured, not guessed.
* ``zipf_keys`` and ``simulate_hit_ratio`` replay a Zipfian key stream through caches of
  different sizes: the "cache 20% of the keys for 80% of the hits" rule, measured.

Public API reused by the distributed-cache case study: ``LRUCache`` (``get``, ``put``,
``delete``, ``keys``, ``stats``, ``__len__``, ``__contains__``), ``CacheStats``, ``zipf_keys``,
``simulate_hit_ratio``. One ``threading.Lock`` guards the dict, the list and the counters, so
one instance can be shared by request threads.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from common import Clock, FakeClock, SystemClock, ValidationError


# --8<-- [start:node]
@dataclass(slots=True)
class _Node:
    """One cache entry, linked into the recency list.

    The list is circular with a sentinel node: ``sentinel.next`` is the most recently used
    entry, ``sentinel.prev`` the least recently used, and a node's neighbours are never None.
    """

    key: Any
    value: Any
    expires_at: float | None
    prev: _Node = field(init=False)
    next: _Node = field(init=False)

    def __post_init__(self) -> None:
        self.prev = self.next = self

    def unlink(self) -> None:
        self.prev.next = self.next
        self.next.prev = self.prev

    def insert_after(self, anchor: _Node) -> None:
        self.prev = anchor
        self.next = anchor.next
        anchor.next.prev = self
        anchor.next = self


@dataclass(frozen=True, slots=True)
class CacheStats:
    hits: int
    misses: int
    evictions: int  # dropped to make room while still valid
    expirations: int  # dropped because their TTL had passed

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


# --8<-- [end:node]


# --8<-- [start:cache]
class LRUCache[K, V]:
    """Least-recently-used cache with optional per-entry TTL; every operation is O(1).

    ``_map`` finds a node by key, the circular list through ``_sentinel`` orders nodes by
    recency. ``_lock`` guards both structures and the counters: the dict and the list must
    change together, and a torn update would leave a node in one but not the other.
    """

    def __init__(self, capacity: int, clock: Clock | None = None) -> None:
        if capacity <= 0:
            raise ValidationError("capacity must be positive")
        self._capacity = capacity
        self._clock = clock or SystemClock()
        self._map: dict[K, _Node] = {}
        self._sentinel = _Node(None, None, None)
        self._lock = threading.Lock()
        self._hits = self._misses = self._evictions = self._expirations = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(self._hits, self._misses, self._evictions, self._expirations)

    def __len__(self) -> int:
        """Entries held, including expired ones that nothing has touched yet."""
        with self._lock:
            return len(self._map)

    def __contains__(self, key: object) -> bool:
        """True for a live entry; does not count as a hit and does not change recency."""
        with self._lock:
            node = self._map.get(key)  # type: ignore[arg-type]
            return node is not None and not self._expired(node)

    def get(self, key: K, default: V | None = None) -> V | None:
        """The value for ``key``, moved to the front; ``default`` on a miss or an expired entry."""
        with self._lock:
            node = self._map.get(key)
            if node is None:
                self._misses += 1
                return default
            if self._expired(node):
                self._drop(node)
                self._expirations += 1
                self._misses += 1
                return default
            node.unlink()
            node.insert_after(self._sentinel)
            self._hits += 1
            return node.value

    def put(self, key: K, value: V, ttl: float | None = None) -> None:
        """Insert or overwrite ``key`` at the front, evicting the back entry when full."""
        if ttl is not None and ttl <= 0:
            raise ValidationError("ttl must be positive")
        expires_at = None if ttl is None else self._clock.now() + ttl
        with self._lock:
            node = self._map.get(key)
            if node is not None:
                node.value = value
                node.expires_at = expires_at
                node.unlink()
                node.insert_after(self._sentinel)
                return
            if len(self._map) >= self._capacity:
                victim = self._sentinel.prev
                self._drop(victim)
                if self._expired(victim):
                    self._expirations += 1
                else:
                    self._evictions += 1
            node = _Node(key, value, expires_at)
            self._map[key] = node
            node.insert_after(self._sentinel)

    def delete(self, key: K) -> bool:
        """Remove ``key`` (cache invalidation); True if it was present."""
        with self._lock:
            node = self._map.pop(key, None)
            if node is None:
                return False
            node.unlink()
            return True

    def keys(self) -> list[K]:
        """Live keys from most to least recently used; a read-only view for tests and demos."""
        with self._lock:
            out: list[K] = []
            node = self._sentinel.next
            while node is not self._sentinel:
                if not self._expired(node):
                    out.append(node.key)
                node = node.next
            return out

    def _expired(self, node: _Node) -> bool:
        return node.expires_at is not None and self._clock.now() >= node.expires_at

    def _drop(self, node: _Node) -> None:
        del self._map[node.key]
        node.unlink()


# --8<-- [end:cache]


# --8<-- [start:simulation]
def zipf_keys(n_keys: int, n_requests: int, exponent: float = 1.0, seed: int = 42) -> list[str]:
    """A key stream where rank r is requested with weight 1/r^exponent (web traffic is ~Zipfian)."""
    if n_keys <= 0 or n_requests < 0:
        raise ValidationError("n_keys must be positive and n_requests non-negative")
    rng = random.Random(seed)
    weights = [1.0 / (rank**exponent) for rank in range(1, n_keys + 1)]
    return [f"key:{i}" for i in rng.choices(range(n_keys), weights=weights, k=n_requests)]


def simulate_hit_ratio(keys: Sequence[str], capacity: int) -> float:
    """Replay ``keys`` through a cache-aside loop and return the measured hit ratio."""
    cache: LRUCache[str, int] = LRUCache(capacity)
    for key in keys:
        if cache.get(key) is None:
            cache.put(key, 1)
    return cache.stats.hit_ratio


# --8<-- [end:simulation]


def main() -> None:
    clock = FakeClock(start=1_000.0)
    cache: LRUCache[str, str] = LRUCache(capacity=3, clock=clock)
    for key in "abc":
        cache.put(key, key.upper())
    print(f"put a, b, c            : order (MRU first) = {cache.keys()}")
    cache.get("a")
    print(f"get a                  : order = {cache.keys()}")
    cache.put("d", "D")
    print(f"put d (full)           : order = {cache.keys()}  evicted b, the LRU entry")
    cache.put("e", "E", ttl=10)
    clock.advance(10)
    print(f"put e ttl=10, +10 s    : get e -> {cache.get('e')!r}; order = {cache.keys()}")
    stats = cache.stats
    print(
        f"stats                  : hits={stats.hits} misses={stats.misses} "
        f"evictions={stats.evictions} expirations={stats.expirations} hit ratio={stats.hit_ratio:.0%}"
    )

    n_keys, n_requests = 10_000, 50_000
    stream = zipf_keys(n_keys, n_requests)
    print(f"zipf(1.0) stream       : {n_keys:,} keys, {n_requests:,} requests")
    for percent in (1, 5, 10, 20):
        capacity = n_keys * percent // 100
        ratio = simulate_hit_ratio(stream, capacity)
        print(f"  cache {percent:>2}% of keys ({capacity:>5,} entries): hit ratio {ratio:.1%}")


if __name__ == "__main__":
    main()
