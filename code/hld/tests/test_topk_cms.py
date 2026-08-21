from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.count_min_sketch import zipf_stream
from hld.topk_cms import WindowedTopK, exact_top_k, merge_top_k, shard_of

BASE = 1_700_000_040.0  # exactly on a minute boundary


@pytest.fixture
def windowed() -> WindowedTopK:
    return WindowedTopK(k=10, bucket_s=60.0, retain=60)


def test_counts_are_bucketed_by_event_time(windowed: WindowedTopK) -> None:
    for i in range(5):
        windowed.add("a", BASE + i)
    for i in range(3):
        windowed.add("b", BASE + 70 + i)
    assert windowed.bucket_count == 2
    assert windowed.top(2, window_s=60) == [("b", 3)]  # only the newest bucket
    assert windowed.top(2) == [("a", 5), ("b", 3)]  # merged over everything retained


def test_a_recent_burst_outranks_a_steady_key_only_in_the_short_window(
    windowed: WindowedTopK,
) -> None:
    for minute in range(10):
        for i in range(20):
            windowed.add("steady", BASE + minute * 60 + i)
    for i in range(60):
        windowed.add("burst", BASE + 9 * 60 + i * 0.5)
    assert [k for k, _ in windowed.top(2, window_s=60)] == ["burst", "steady"]
    assert [k for k, _ in windowed.top(2)] == ["steady", "burst"]  # 200 beats 60 over 10 minutes


def test_buckets_outside_the_retention_horizon_are_dropped() -> None:
    windowed = WindowedTopK(k=5, bucket_s=60.0, retain=3)
    for minute in range(10):
        windowed.add(f"m{minute}", BASE + minute * 60)
    assert windowed.bucket_count == 3
    assert {k for k, _ in windowed.top(5)} == {"m7", "m8", "m9"}
    assert windowed.memory_bytes() == 3 * windowed.bucket_shape[2]  # memory tracks buckets, not keys


def test_estimates_never_undercount_on_a_skewed_stream() -> None:
    windowed = WindowedTopK(k=20, bucket_s=60.0, retain=60, epsilon=0.001, delta=0.01)
    stream = zipf_stream(keys=2_000, events=20_000, seed=7)
    exact = Counter(stream)
    for i, key in enumerate(stream):
        windowed.add(key, BASE + i * 0.01)  # 200 s -> a few buckets
    approx = windowed.top(10)
    assert [k for k, _ in approx] == [k for k, _ in exact_top_k(stream, 10)]
    for key, estimate in approx:
        assert exact[key] <= estimate <= exact[key] + 20_000 * 0.001 + 1


def test_shard_merge_matches_a_single_unsharded_index() -> None:
    stream = zipf_stream(keys=500, events=5_000, seed=11)
    shards = [WindowedTopK(k=20, bucket_s=60.0, retain=60) for _ in range(4)]
    single = WindowedTopK(k=20, bucket_s=60.0, retain=60)
    for i, key in enumerate(stream):
        shards[shard_of(key, 4)].add(key, BASE + i * 0.01)
        single.add(key, BASE + i * 0.01)
    merged = merge_top_k((s.top(20) for s in shards), 5)
    assert [k for k, _ in merged] == [k for k, _ in exact_top_k(stream, 5)]
    assert [k for k, _ in merged] == [k for k, _ in single.top(5)]
    assert len({shard_of(k, 4) for k, _ in merged}) > 1  # the winners really are spread out


def test_merge_top_k_sums_across_partials_and_breaks_ties_deterministically() -> None:
    partials = [[("a", 5), ("b", 3)], [("b", 4), ("c", 5)], [("a", 5), ("c", 5)]]
    assert merge_top_k(partials, 3) == [("a", 10), ("c", 10), ("b", 7)]  # ties sorted by key
    assert merge_top_k([], 3) == []


def test_validation(windowed: WindowedTopK) -> None:
    with pytest.raises(ValidationError):
        WindowedTopK(k=0)
    with pytest.raises(ValidationError):
        WindowedTopK(bucket_s=0)
    with pytest.raises(ValidationError):
        windowed.top(0)
    with pytest.raises(ValidationError):
        merge_top_k([], 0)
    with pytest.raises(ValidationError):
        exact_top_k(["a"], 0)
    with pytest.raises(ValidationError):
        shard_of("a", 0)
    assert windowed.top(5) == []  # no buckets yet


def test_concurrent_adds_land_in_the_right_buckets() -> None:
    windowed = WindowedTopK(k=10, bucket_s=60.0, retain=60)

    def work(i: int) -> None:
        windowed.add(f"key{i % 4}", BASE + (i % 3) * 60)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(4_000)))
    assert windowed.bucket_count == 3
    totals = dict(windowed.top(4))
    assert sorted(totals) == ["key0", "key1", "key2", "key3"]
    assert sum(totals.values()) == 4_000  # nothing lost, nothing double counted
