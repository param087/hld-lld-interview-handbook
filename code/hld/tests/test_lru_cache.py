from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.lru_cache import CacheStats, LRUCache, simulate_hit_ratio, zipf_keys


def test_eviction_drops_the_least_recently_used_entry() -> None:
    cache: LRUCache[str, int] = LRUCache(capacity=3)
    for i, key in enumerate("abc"):
        cache.put(key, i)
    assert cache.keys() == ["c", "b", "a"]
    assert cache.get("a") == 0  # a read moves the entry to the front
    cache.put("b", 10)  # an overwrite does too
    assert cache.keys() == ["b", "a", "c"]
    cache.put("d", 3)
    assert cache.keys() == ["d", "b", "a"]
    assert cache.get("c") is None
    assert len(cache) == 3
    assert cache.stats == CacheStats(hits=1, misses=1, evictions=1, expirations=0)


def test_ttl_expiry_is_lazy_and_uses_the_injected_clock() -> None:
    clock = FakeClock(start=100.0)
    cache: LRUCache[str, str] = LRUCache(capacity=2, clock=clock)
    cache.put("k", "v", ttl=5)
    clock.advance(4.9)
    assert cache.get("k") == "v" and "k" in cache
    clock.advance(0.1)
    assert "k" not in cache
    assert len(cache) == 1  # held until something touches it
    assert cache.get("k", "default") == "default"
    assert len(cache) == 0
    assert cache.stats.expirations == 1 and cache.stats.misses == 1
    cache.put("k", "v2", ttl=5)
    cache.put("k", "v3")  # overwriting without a ttl clears the expiry
    clock.advance(100)
    assert cache.get("k") == "v3"


def test_expired_victim_counts_as_an_expiration_not_an_eviction() -> None:
    clock = FakeClock()
    cache: LRUCache[str, int] = LRUCache(capacity=1, clock=clock)
    cache.put("a", 1, ttl=1)
    clock.advance(2)
    cache.put("b", 2)
    assert cache.stats.expirations == 1 and cache.stats.evictions == 0
    assert cache.keys() == ["b"]


def test_delete_and_validation() -> None:
    cache: LRUCache[str, int] = LRUCache(capacity=2)
    cache.put("a", 1)
    assert cache.delete("a") is True
    assert cache.delete("a") is False
    assert cache.get("a") is None and len(cache) == 0
    with pytest.raises(ValidationError):
        LRUCache(capacity=0)
    with pytest.raises(ValidationError):
        cache.put("x", 1, ttl=0)
    with pytest.raises(ValidationError):
        zipf_keys(0, 10)


def test_zipf_hit_ratio_grows_with_capacity() -> None:
    stream = zipf_keys(n_keys=2_000, n_requests=20_000, seed=1)
    assert len(stream) == 20_000 and len(set(stream)) <= 2_000
    small = simulate_hit_ratio(stream, capacity=20)
    medium = simulate_hit_ratio(stream, capacity=200)
    large = simulate_hit_ratio(stream, capacity=2_000)
    assert 0 < small < medium < large < 1
    assert large > 0.8  # only compulsory misses remain when every key fits


def test_concurrent_puts_and_gets_keep_the_dict_and_list_consistent() -> None:
    cache: LRUCache[int, int] = LRUCache(capacity=64)

    def worker(seed: int) -> int:
        hits = 0
        for i in range(2_000):
            key = (seed * 7 + i) % 100
            if cache.get(key) is None:
                cache.put(key, key)
            else:
                hits += 1
        return hits

    with ThreadPoolExecutor(max_workers=8) as pool:
        worker_hits = sum(pool.map(worker, range(8)))
    keys = cache.keys()
    assert len(keys) == len(cache) <= 64
    assert len(set(keys)) == len(keys)
    assert all(cache.get(key) == key for key in keys)
    stats = cache.stats
    assert stats.hits == worker_hits + len(keys)
    assert stats.hits + stats.misses == 8 * 2_000 + len(keys)
