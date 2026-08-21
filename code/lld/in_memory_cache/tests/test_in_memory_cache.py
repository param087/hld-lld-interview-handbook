import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from lld.in_memory_cache.models import CacheStats, CapacityError, KeyMissingError
from lld.in_memory_cache.policies import FIFOPolicy, LFUPolicy, LRUPolicy, make_policy
from lld.in_memory_cache.services import Cache, LoadingCache, ShardedCache, TtlSweeper

POLL_SECONDS = 0.002
POLL_ATTEMPTS = 50


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000)


def wait_until(predicate: Callable[[], bool]) -> bool:
    for _ in range(POLL_ATTEMPTS):
        if predicate():
            return True
        time.sleep(POLL_SECONDS)
    return predicate()


def fill(cache: Cache[str, int], keys: str) -> None:
    for i, key in enumerate(keys):
        cache.put(key, i)


def test_lru_evicts_the_least_recently_used_key(clock: FakeClock) -> None:
    cache: Cache[str, int] = Cache(capacity=3, policy=LRUPolicy(), clock=clock)
    fill(cache, "abc")
    assert cache.get("a") == 0  # a becomes the most recent, b becomes the victim
    cache.put("d", 3)
    assert cache.get("b") is None
    assert cache.eviction_order() == ["c", "a", "d"]
    assert cache.stats() == CacheStats(hits=1, misses=1, evictions=1, size=3, capacity=3)


def test_lfu_evicts_the_least_used_and_breaks_ties_by_recency(clock: FakeClock) -> None:
    cache: Cache[str, int] = Cache(capacity=3, policy=LFUPolicy(), clock=clock)
    fill(cache, "abc")
    for _ in range(3):
        cache.get("a")
    cache.get("b")
    cache.put("d", 3)  # c is the only key still at frequency 1
    assert cache.get("c") is None
    assert cache.eviction_order() == ["d", "b", "a"]
    cache.get("d")  # now d and b are both at frequency 2; b arrived there first
    cache.put("e", 4)
    assert cache.get("b") is None and cache.get("d") == 3


@pytest.mark.parametrize(
    ("policy_name", "evicted", "kept"),
    [("lru", "b", "a"), ("fifo", "a", "b"), ("lfu", "b", "a")],
)
def test_policies_disagree_about_a_read_key(clock: FakeClock, policy_name: str, evicted: str, kept: str) -> None:
    """a is written first and then read; only FIFO still throws it out first."""
    cache: Cache[str, int] = Cache(capacity=2, policy=make_policy(policy_name), clock=clock)
    fill(cache, "ab")
    cache.get("a")
    cache.put("c", 2)
    assert cache.get(evicted) is None
    assert cache.get(kept) is not None


def test_ttl_expires_lazily_and_is_counted_as_an_expiration(clock: FakeClock) -> None:
    cache: Cache[str, str] = Cache(capacity=10, clock=clock, default_ttl=300.0)
    cache.put("session:42", "user-7")
    cache.put("forever", "x", ttl=None)
    clock.advance(299)
    assert cache["session:42"] == "user-7" and "session:42" in cache
    clock.advance(2)
    assert "session:42" not in cache  # membership never resurrects an expired key
    assert cache.get("session:42") is None
    with pytest.raises(KeyMissingError):
        _ = cache["session:42"]
    stats = cache.stats()
    assert stats.expirations == 1 and stats.evictions == 0 and stats.size == 1


def test_active_purge_reclaims_without_a_read(clock: FakeClock) -> None:
    cache: Cache[str, int] = Cache(capacity=10, clock=clock, default_ttl=60.0)
    fill(cache, "abc")
    cache.put("d", 3, ttl=600.0)
    clock.advance(61)
    assert cache.purge_expired() == 3
    assert cache.keys() == ["d"] and cache.eviction_order() == ["d"]


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"capacity": 0}, CapacityError),
        ({"capacity": -1}, CapacityError),
        ({"capacity": 4, "default_ttl": 0.0}, ValidationError),
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        Cache(**kwargs)  # type: ignore[arg-type]


def test_invalid_ttl_and_unknown_policy_are_rejected(clock: FakeClock) -> None:
    cache: Cache[str, int] = Cache(capacity=2, clock=clock)
    with pytest.raises(ValidationError):
        cache.put("a", 1, ttl=0)
    with pytest.raises(ValidationError):
        make_policy("lru-k")


def test_delete_and_overwrite_keep_the_two_structures_in_step(clock: FakeClock) -> None:
    cache: Cache[str, int] = Cache(capacity=3, policy=FIFOPolicy(), clock=clock)
    fill(cache, "abc")
    assert cache.delete("b") is True and cache.delete("b") is False
    cache.put("a", 99)  # an overwrite must not insert a second node
    cache.put("d", 3)
    assert set(cache.keys()) == set(cache.eviction_order()) == {"a", "c", "d"}
    assert len(cache) == 3 and cache.get("a") == 99


# --8<-- [start:concurrency]
def test_concurrent_writers_never_break_the_capacity_invariant(clock: FakeClock) -> None:
    """8 threads, 800 distinct keys, capacity 50: the two structures must not drift.

    Without the lock, `len(entries) >= capacity` and the eviction that follows are
    two steps: two threads can both read 49, both skip the eviction and both
    insert - leaving 51 entries and, worse, a policy that no longer lists the same
    keys the dict holds.
    """
    cache: Cache[str, int] = Cache(capacity=50, policy=LRUPolicy(), clock=clock)

    def write(i: int) -> None:
        cache.put(f"key-{i}", i)
        cache.get(f"key-{i // 2}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(800)))

    assert len(cache) == 50
    assert set(cache.keys()) == set(cache.eviction_order())
    stats = cache.stats()
    assert stats.lookups == 800  # no counter update was lost
    assert stats.evictions == 750


# --8<-- [end:concurrency]


# --8<-- [start:single_flight]
def test_single_flight_loads_a_hot_key_once(clock: FakeClock) -> None:
    """The stampede test: 16 threads miss the same key, the loader runs once."""
    calls: list[str] = []
    gate = threading.Barrier(16)

    def loader(key: str) -> str:
        calls.append(key)
        time.sleep(0.01)  # a slow query is what makes a stampede expensive
        return f"row({key})"

    cache = LoadingCache(Cache[str, str](capacity=8, clock=clock), loader=loader)

    def read(_: int) -> str:
        gate.wait()
        return cache.get("product:1")

    with ThreadPoolExecutor(max_workers=16) as pool:
        values = set(pool.map(read, range(16)))

    assert values == {"row(product:1)"}
    assert calls == ["product:1"]  # exactly one database round trip
    stats = cache.stats()
    # one winner ran the loader; the other 15 re-checked under the stripe and found it
    assert stats.loads == 1 and stats.coalesced == 15 and stats.misses == 17


# --8<-- [end:single_flight]


def test_loader_failures_are_not_cached(clock: FakeClock) -> None:
    attempts: list[str] = []

    def flaky(key: str) -> str:
        attempts.append(key)
        if len(attempts) == 1:
            raise TimeoutError("database is busy")
        return "ok"

    cache = LoadingCache(Cache[str, str](capacity=4, clock=clock), loader=flaky)
    with pytest.raises(TimeoutError):
        cache.get("k")
    assert cache.get("k") == "ok" and attempts == ["k", "k"]


def test_sweeper_reclaims_in_the_background_and_stops_promptly(clock: FakeClock) -> None:
    cache: Cache[str, int] = Cache(capacity=10, clock=clock, default_ttl=30.0)
    fill(cache, "ab")
    sweeper = TtlSweeper(cache, interval=0.002)
    sweeper.start()
    clock.advance(31)
    assert wait_until(lambda: sweeper.purged >= 2)
    sweeper.stop(timeout=0.5)
    assert len(cache) == 0 and cache.stats().expirations == 2


def test_sharded_cache_splits_capacity_and_stays_consistent(clock: FakeClock) -> None:
    cache: ShardedCache[str, int] = ShardedCache(capacity=64, shards=8, clock=clock)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: cache.put(f"k{i}", i), range(400)))
    assert len(cache) == 64 and cache.stats().capacity == 64
    with pytest.raises(CapacityError):
        ShardedCache(capacity=4, shards=8)
