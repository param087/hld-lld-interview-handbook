"""Distributed cache: consistent-hash routing, per-node LRU with TTL, and Memcache leases.

What the module demonstrates, in the order an interviewer asks about it:

* ``CacheCluster`` is the *client-side router*. Keys are placed with the ``HashRing`` of the
  partitioning page, so a get costs one network hop and adding a node moves only ~1/N of the
  keys; ``fail`` and ``recover`` show what a node loss does to the hit ratio.
* ``CacheNode`` is one server: an ``LRUCache`` from the caching page (dict plus linked list,
  lazy TTL check on read) alongside a Redis-style ``expires`` registry that ``active_expire``
  samples, because lazy expiry alone leaves dead entries occupying memory nobody reclaims.
* **Leases** are the distributed answer to the thundering herd. On a miss the first client gets
  a token and loads from the database; later clients are told to wait or are served a stale
  value; and a ``set`` whose token an invalidation has revoked is rejected, which is what stops
  a slow client from writing back a value the database has already changed (the *stale set*).
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from common import (
    Clock,
    FakeClock,
    IdGenerator,
    InvalidStateError,
    NotFoundError,
    SequentialIdGenerator,
    SystemClock,
    ValidationError,
)
from hld.consistent_hashing import HashRing, assignments, keys_moved
from hld.lru_cache import CacheStats, LRUCache


# --8<-- [start:lease]
class CacheOutcome(StrEnum):
    """What a ``get`` tells the client to do next."""

    HIT = "hit"  # use the value
    LEASE = "lease"  # you own the recompute: load from the database and set with this token
    WAIT = "wait"  # another client holds the lease: back off, or use the stale value


@dataclass(frozen=True, slots=True)
class Lease:
    """Permission to fill one key. Only the holder's ``set`` is accepted."""

    key: str
    token: str
    issued_at: float


@dataclass(frozen=True, slots=True)
class GetResult:
    outcome: CacheOutcome
    value: str | None  # the cached value on a hit, a stale value on a miss, else None
    lease: Lease | None


class CacheNode:
    """One cache server: an LRU with TTL, an expiry registry and the lease table.

    ``_lock`` guards the lease table, the ``_expires`` registry and the stale copies; the
    wrapped ``LRUCache`` has its own lock. Entries evicted by the LRU leave their ``_expires``
    row behind, so the sweep drops rows whose key is already gone: the registry is a hint,
    never the source of truth (Redis keeps exactly such a separate ``expires`` dict).
    """

    def __init__(
        self,
        node_id: str,
        capacity: int,
        *,
        lease_ttl: float = 10.0,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        if lease_ttl <= 0:
            raise ValidationError("lease_ttl must be positive")
        self.node_id = node_id
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator(f"{node_id}-lease")
        self._lease_ttl = lease_ttl
        self._store: LRUCache[str, str] = LRUCache(capacity, self._clock)
        self._expires: dict[str, float] = {}
        self._leases: dict[str, Lease] = {}
        self._stale: dict[str, str] = {}
        self._lock = threading.Lock()
        self._rejected_sets = 0

    @property
    def stats(self) -> CacheStats:
        return self._store.stats

    @property
    def rejected_sets(self) -> int:
        """Sets refused because an invalidation revoked the lease behind them."""
        with self._lock:
            return self._rejected_sets

    def __len__(self) -> int:
        """Entries occupying memory, including expired ones nothing has touched."""
        return len(self._store)

    def live_keys(self) -> list[str]:
        return self._store.keys()

    def get(self, key: str) -> GetResult:
        """Hit, or a lease for the first client to miss, or a wait for everyone behind it."""
        value = self._store.get(key)
        if value is not None:
            return GetResult(CacheOutcome.HIT, value, None)
        now = self._clock.now()
        with self._lock:
            held = self._leases.get(key)
            if held is not None and now - held.issued_at < self._lease_ttl:
                return GetResult(CacheOutcome.WAIT, self._stale.get(key), None)
            lease = Lease(key, self._ids.next_id(), now)
            self._leases[key] = lease
            return GetResult(CacheOutcome.LEASE, self._stale.get(key), lease)

    def set(self, key: str, value: str, lease: Lease, ttl: float | None = None) -> bool:
        """Fill ``key`` if ``lease`` is still the live one; False means the write was refused."""
        with self._lock:
            held = self._leases.get(key)
            if held is None or held.token != lease.token:
                self._rejected_sets += 1
                return False
            del self._leases[key]
            self._stale.pop(key, None)
            self._remember_ttl(key, ttl)
        self._store.put(key, value, ttl)
        return True

    def put(self, key: str, value: str, ttl: float | None = None) -> None:
        """Unconditional write: the write-through path, which owns the value already."""
        with self._lock:
            self._leases.pop(key, None)  # a filler based on the old value must not win
            self._stale.pop(key, None)
            self._remember_ttl(key, ttl)
        self._store.put(key, value, ttl)

    def invalidate(self, key: str, *, keep_stale: bool = False) -> None:
        """Delete ``key`` and revoke outstanding leases, so no in-flight fill can resurrect it."""
        with self._lock:
            self._leases.pop(key, None)
            self._expires.pop(key, None)
            if keep_stale:
                current = self._store.get(key)
                if current is not None:
                    self._stale[key] = current
            else:
                self._stale.pop(key, None)
        self._store.delete(key)

    def active_expire(self, sample: int, rng: random.Random) -> int:
        """Redis-style sampled sweep: check ``sample`` random keys and drop the expired ones.

        Lazy expiry (checking the TTL on read) is enough for correctness but not for memory: a
        key nobody reads again is never noticed. A sampled sweep bounds the work per cycle
        instead of scanning every key.
        """
        if sample <= 0:
            raise ValidationError("sample must be positive")
        now = self._clock.now()
        with self._lock:
            candidates = list(self._expires)
            chosen = rng.sample(candidates, min(sample, len(candidates)))
            expired = [key for key in chosen if self._expires[key] <= now]
            for key in expired:
                del self._expires[key]
        # a key the LRU already evicted leaves a registry row behind and reclaims nothing
        return sum(1 for key in expired if self._store.delete(key))

    def _remember_ttl(self, key: str, ttl: float | None) -> None:
        """Caller holds ``_lock``."""
        if ttl is None:
            self._expires.pop(key, None)
        else:
            self._expires[key] = self._clock.now() + ttl


# --8<-- [end:lease]


# --8<-- [start:cluster]
class CacheCluster:
    """A client-side router over cache servers, with replicas for failover.

    The ring lives in the *client*: a get is one hop to the right server, where a proxy
    (mcrouter, Twemproxy) would cost a second hop but keep the topology in one place.
    ``_lock`` guards the down set; the ring is copy-on-write and each node locks its own state.
    """

    def __init__(
        self,
        nodes: Sequence[str],
        *,
        capacity_per_node: int = 1024,
        vnodes: int = 100,
        replicas: int = 2,
        lease_ttl: float = 10.0,
        wait: float = 0.0005,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        if not nodes:
            raise ValidationError("a cluster needs at least one node")
        if not 1 <= replicas <= len(nodes):
            raise ValidationError("replicas must be between 1 and the node count")
        self._clock = clock or SystemClock()
        self._replicas = replicas
        self._wait = wait
        self._ring = HashRing(nodes, vnodes=vnodes)
        self._nodes = {
            node: CacheNode(node, capacity_per_node, lease_ttl=lease_ttl, clock=self._clock, ids=ids)
            for node in nodes
        }
        self._down: set[str] = set()
        self._lock = threading.Lock()

    @property
    def nodes(self) -> list[str]:
        return self._ring.nodes

    def node_for(self, key: str) -> str:
        """The first healthy node in the key's preference list: failover is one ring hop."""
        with self._lock:
            for candidate in self._ring.preference_list(key, self._replicas):
                if candidate not in self._down:
                    return candidate
        raise InvalidStateError(f"every replica of {key!r} is down")

    def node(self, node_id: str) -> CacheNode:
        if node_id not in self._nodes:
            raise NotFoundError(f"unknown node {node_id!r}")
        return self._nodes[node_id]

    def get(self, key: str) -> GetResult:
        return self.node(self.node_for(key)).get(key)

    def put(self, key: str, value: str, ttl: float | None = None) -> None:
        self.node(self.node_for(key)).put(key, value, ttl)

    def invalidate(self, key: str, *, keep_stale: bool = False) -> None:
        """Invalidate on the write path: delete beats update, because a delete cannot be stale."""
        self.node(self.node_for(key)).invalidate(key, keep_stale=keep_stale)

    def load_through(
        self,
        key: str,
        loader: Callable[[str], str],
        *,
        ttl: float | None = None,
        max_attempts: int = 100,
    ) -> str:
        """Cache-aside with leases: exactly one caller per key reaches ``loader``.

        A caller told to wait retries; if the key carries a stale copy it takes that instead of
        queueing, which is how a hot key survives an invalidation without a database stampede.
        """
        for _ in range(max_attempts):
            node = self.node(self.node_for(key))
            result = node.get(key)
            if result.outcome is CacheOutcome.HIT:
                return result.value or ""
            if result.lease is not None:
                value = loader(key)
                node.set(key, value, result.lease, ttl)
                return value
            if result.value is not None:
                return result.value  # serve stale rather than pile onto the database
            time.sleep(self._wait)
        raise InvalidStateError(f"gave up waiting for the holder of the lease on {key!r}")

    def fail(self, node_id: str) -> None:
        """Take a node out of rotation; its keys fall to the next replica, which is cold."""
        self.node(node_id)
        with self._lock:
            self._down.add(node_id)

    def recover(self, node_id: str) -> None:
        self.node(node_id)
        with self._lock:
            self._down.discard(node_id)

    def add_node(self, node_id: str, keys: Iterable[str], capacity: int = 1024) -> float:
        """Add a node and report the fraction of ``keys`` that changed owner."""
        before = assignments(self._ring, keys)
        self._ring.add_node(node_id)
        self._nodes[node_id] = CacheNode(node_id, capacity, clock=self._clock)
        return keys_moved(before, assignments(self._ring, keys)).fraction

    def hit_ratio(self) -> float:
        totals = [node.stats for node in self._nodes.values()]
        hits = sum(stat.hits for stat in totals)
        requests = hits + sum(stat.misses for stat in totals)
        return hits / requests if requests else 0.0


# --8<-- [end:cluster]


def main() -> None:
    from concurrent.futures import ThreadPoolExecutor

    clock = FakeClock(start=1_000.0)
    keys = [f"user:{i}" for i in range(10_000)]
    cluster = CacheCluster(
        ["c1", "c2", "c3", "c4"], capacity_per_node=500, replicas=2, clock=clock
    )
    placement = [f"{key} -> {cluster.node_for(key)}" for key in ("user:1", "user:2", "user:3")]
    print(f"routing (4 nodes x 100 vnodes): {', '.join(placement)}")
    moved = cluster.add_node("c5", keys)
    print(f"add c5                        : {moved:.1%} of 10,000 keys moved (expected ~1/5)")

    node = cluster.node("c1")
    for i in range(600):
        node.put(f"cold:{i}", "v", ttl=30)
    print(f"600 puts into a 500-entry node: {len(node)} entries held, {node.stats.evictions} evicted")
    clock.advance(60)
    print(
        f"+60 s, TTL 30 s               : {len(node)} entries still held, "
        f"{len(node.live_keys())} live (lazy expiry frees nothing until a read)"
    )
    freed = node.active_expire(sample=100, rng=random.Random(42))
    print(f"active expiry, 100 samples    : {freed} entries reclaimed, {len(node)} held")

    loads: list[str] = []

    def database(key: str) -> str:
        loads.append(key)
        return f"row:{key}"

    readers = 32
    with ThreadPoolExecutor(max_workers=readers) as pool:
        values = list(pool.map(lambda _: cluster.load_through("hot:1", database, ttl=60), range(readers)))
    print(
        f"{readers} clients on one cold key    : {len(loads)} database load, "
        f"all {len(values)} got {set(values).pop()!r}"
    )

    hot = cluster.node(cluster.node_for("stock:1"))
    slow_client = hot.get("stock:1").lease  # client A misses and takes the lease
    hot.invalidate("stock:1")  # a writer changes the database, revoking A's lease
    late = hot.set("stock:1", "price-from-a-stale-read", slow_client, 60) if slow_client else False
    print(f"invalidate then a late fill   : accepted={late}, rejected sets {hot.rejected_sets}")

    hot.put("stock:2", "101", ttl=60)
    hot.invalidate("stock:2", keep_stale=True)
    served = hot.get("stock:2")
    print(f"invalidate, keep a stale copy : {served.outcome}, serve {served.value!r} while one client refills")

    owner = cluster.node_for("user:42")
    cluster.fail(owner)
    print(
        f"{owner} fails, keys shift          : user:42 now on {cluster.node_for('user:42')}, cold, "
        f"so the database sees the miss; cluster hit ratio {cluster.hit_ratio():.0%}"
    )


if __name__ == "__main__":
    main()
