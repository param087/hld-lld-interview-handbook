import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.cache_stampede import CachedLoader, LoaderStats, should_refresh_early


def wait_for_waiters(loader: CachedLoader[str, str], count: int) -> None:
    deadline = time.monotonic() + 5
    while loader.stats.coalesced < count and time.monotonic() < deadline:
        time.sleep(0.001)


def test_single_flight_turns_concurrent_misses_into_one_load() -> None:
    readers = 16
    release = threading.Event()
    calls: list[str] = []

    def loader(key: str) -> str:
        calls.append(key)
        assert release.wait(timeout=5)
        return f"value:{key}"

    guarded: CachedLoader[str, str] = CachedLoader(loader, ttl=60, beta=0)
    with ThreadPoolExecutor(max_workers=readers) as pool:
        futures = [pool.submit(guarded.get, "k") for _ in range(readers)]
        wait_for_waiters(guarded, readers - 1)
        release.set()
        results = [future.result() for future in futures]
    assert results == ["value:k"] * readers
    assert calls == ["k"]
    assert guarded.stats == LoaderStats(
        hits=0, misses=readers, loads=1, coalesced=readers - 1, early_refreshes=0
    )
    assert guarded.get("k") == "value:k" and guarded.stats.hits == 1


def test_without_coalescing_every_concurrent_miss_loads() -> None:
    readers = 8
    barrier = threading.Barrier(readers)

    def loader(key: str) -> str:
        barrier.wait(timeout=5)  # all readers are inside the loader at once
        return key

    naive: CachedLoader[str, str] = CachedLoader(loader, ttl=60, coalesce=False, beta=0)
    with ThreadPoolExecutor(max_workers=readers) as pool:
        assert list(pool.map(naive.get, ["k"] * readers)) == ["k"] * readers
    assert naive.stats.loads == readers and naive.stats.coalesced == 0


@pytest.mark.parametrize("remaining", [10.0, 2.0, 0.5])
def test_early_refresh_probability_follows_xfetch(remaining: float) -> None:
    rng = random.Random(3)
    delta, beta, draws = 2.0, 1.0, 20_000
    observed = sum(should_refresh_early(0.0, remaining, delta, beta, rng) for _ in range(draws))
    assert abs(observed / draws - math.exp(-remaining / (delta * beta))) < 0.02
    assert not any(should_refresh_early(0.0, remaining, delta, 0.0, rng) for _ in range(100))
    assert should_refresh_early(5.0, 5.0, delta, beta, rng)  # at expiry: always


def test_early_expiration_hides_expiry_from_readers() -> None:
    def run(beta: float) -> LoaderStats:
        clock = FakeClock()

        def db(key: str) -> str:
            clock.advance(0.5)
            return key.upper()

        loader: CachedLoader[str, str] = CachedLoader(
            db, ttl=10, beta=beta, clock=clock, rng=random.Random(7)
        )
        for _ in range(3_000):
            assert loader.get("k") == "K"
            clock.advance(0.01)
        return loader.stats

    plain, early = run(0.0), run(1.0)
    assert plain.misses == plain.loads == 3 and plain.early_refreshes == 0
    assert early.misses == 1 and early.early_refreshes >= 2
    assert early.loads == 1 + early.early_refreshes


def test_failed_load_releases_waiters_and_the_next_caller_retries() -> None:
    attempts = 0
    release = threading.Event()

    def flaky(key: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            assert release.wait(timeout=5)
            raise ConnectionError("db down")
        return "ok"

    loader: CachedLoader[str, str] = CachedLoader(flaky, ttl=60, beta=0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(loader.get, "k") for _ in range(4)]
        wait_for_waiters(loader, 3)
        release.set()
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except ConnectionError:
                outcomes.append("error")
    assert sorted(outcomes) == ["error", "ok", "ok", "ok"]
    assert attempts == 2 and loader.stats.loads == 2
    assert loader.get("k") == "ok" and loader.stats.loads == 2


def test_invalidate_and_ttl_force_reloads() -> None:
    clock = FakeClock()
    values = iter(["v1", "v2", "v3"])
    loader: CachedLoader[str, str] = CachedLoader(
        lambda key: next(values), ttl=10, beta=0, clock=clock
    )
    assert loader.get("k") == "v1" and loader.get("k") == "v1"
    loader.invalidate("k")
    assert loader.get("k") == "v2"
    clock.advance(10)
    assert loader.get("k") == "v3"
    assert loader.stats == LoaderStats(hits=1, misses=3, loads=3, coalesced=0, early_refreshes=0)


@pytest.mark.parametrize(
    "kwargs", [{"ttl": 0}, {"ttl": 10, "beta": -1}, {"ttl": 10, "capacity": 0}]
)
def test_constructor_validation(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        CachedLoader(str, **kwargs)
