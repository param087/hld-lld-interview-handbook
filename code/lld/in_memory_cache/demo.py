"""One scenario per feature: LRU vs LFU order, TTL, the sweeper, single flight, sharding."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from common import FakeClock
from lld.in_memory_cache.policies import LFUPolicy, LRUPolicy
from lld.in_memory_cache.services import Cache, LoadingCache, ShardedCache, TtlSweeper

POLL_SECONDS = 0.002
POLL_ATTEMPTS = 100
READERS = 8
LOAD_SECONDS = 0.01


def wait_until(predicate: Callable[[], bool]) -> bool:
    """Bounded wait for a background thread; 100 polls of 2 ms at the very worst."""
    for _ in range(POLL_ATTEMPTS):
        if predicate():
            return True
        time.sleep(POLL_SECONDS)
    return predicate()


def show_order(label: str, cache: Cache[str, int]) -> None:
    print(f"  {label}: {[str(k) for k in cache.eviction_order()]}")


def main() -> None:
    clock = FakeClock(start=1_700_000_000)

    print("--- LRU, capacity 3: put a,b,c then read a, then put d ---")
    lru: Cache[str, int] = Cache(capacity=3, policy=LRUPolicy(), clock=clock)
    for i, key in enumerate("abc"):
        lru.put(key, i)
    lru.get("a")
    lru.put("d", 3)
    show_order("eviction order, victim first", lru)
    print(f"  b was evicted: get('b') -> {lru.get('b')}, stats: {lru.stats()}")

    print("--- LFU, capacity 3: a read 3 times, b once, c never, then put d ---")
    lfu: Cache[str, int] = Cache(capacity=3, policy=LFUPolicy(), clock=clock)
    for i, key in enumerate("abc"):
        lfu.put(key, i)
    for _ in range(3):
        lfu.get("a")
    lfu.get("b")
    lfu.put("d", 3)
    show_order("eviction order, victim first", lfu)
    print(f"  c was evicted (least used): get('c') -> {lfu.get('c')}")

    print("--- TTL with lazy expiry ---")
    sessions: Cache[str, str] = Cache(capacity=100, clock=clock, default_ttl=300.0)
    sessions.put("session:42", "user-7")
    clock.advance(240)
    print(f"  after 4 min: {sessions.get('session:42')}")
    clock.advance(120)
    print(f"  after 6 min: {sessions.get('session:42')}, {sessions.stats()}")

    print("--- background sweeper reclaims what nobody reads again ---")
    swept: Cache[str, str] = Cache(capacity=100, clock=clock, default_ttl=60.0)
    swept.put("tmp:1", "x")
    swept.put("tmp:2", "y")
    sweeper = TtlSweeper(swept, interval=0.005)
    sweeper.start()
    clock.advance(90)
    reclaimed = wait_until(lambda: sweeper.purged >= 2)
    sweeper.stop()
    print(f"  reclaimed both keys without a read: {reclaimed}, cache size now {len(swept)}")

    print("--- loading cache: 8 threads, one cold key, one database query ---")
    calls: list[str] = []
    gate = threading.Barrier(READERS)

    def load(key: str) -> str:
        calls.append(key)  # list.append is atomic; one entry per real database round trip
        time.sleep(LOAD_SECONDS)  # stand-in for the query the stampede would repeat
        return f"row({key})"

    def read(_: int) -> str:
        gate.wait()  # every thread asks at the same instant, as a stampede does
        return loading.get("product:1")

    loading = LoadingCache(Cache[str, str](capacity=10, clock=clock), loader=load)
    with ThreadPoolExecutor(max_workers=READERS) as pool:
        values = set(pool.map(read, range(READERS)))
    stats = loading.stats()
    print(f"  values agree: {values}, loader ran {len(calls)} time(s), coalesced={stats.coalesced}")

    print("--- sharded cache: 8 locks instead of 1 ---")
    sharded: ShardedCache[str, int] = ShardedCache(capacity=64, shards=8, clock=clock)
    with ThreadPoolExecutor(max_workers=8) as pool:
        pool.map(lambda i: sharded.put(f"k{i}", i), range(400))
    print(f"  400 keys written by 8 threads, capacity 64 -> size {len(sharded)}")
    print(f"  {sharded.stats()}")


if __name__ == "__main__":
    main()
