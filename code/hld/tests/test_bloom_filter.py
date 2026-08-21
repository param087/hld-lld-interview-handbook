import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.bloom_filter import (
    BloomFilter,
    CountingBloomFilter,
    false_positive_rate,
    optimal_size,
    positions,
)


def random_items(prefix: str, n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return [f"{prefix}:{rng.getrandbits(64):016x}" for _ in range(n)]


def test_sizing_formulas() -> None:
    assert optimal_size(10_000, 0.01) == (95_851, 7)
    assert optimal_size(1_000_000, 0.001) == (14_377_588, 10)
    bits, hashes = optimal_size(5_000, 0.05)
    assert false_positive_rate(bits, hashes, 5_000) == pytest.approx(0.05, rel=0.05)
    assert false_positive_rate(bits, hashes, 0) == 0.0
    assert false_positive_rate(bits, hashes, 50_000) > 0.9


def test_positions_are_deterministic_in_range_and_spread() -> None:
    first = list(positions("user:42", 7, 95_851))
    assert first == list(positions("user:42", 7, 95_851))
    assert all(0 <= pos < 95_851 for pos in first)
    assert len(set(first)) == 7
    assert first != list(positions("user:43", 7, 95_851))


def test_no_false_negatives_ever() -> None:
    bloom = BloomFilter(capacity=2_000, error_rate=0.01)
    members = random_items("m", 2_000, seed=1)
    for item in members:
        bloom.add(item)
    assert all(item in bloom for item in members)
    assert len(bloom) == 2_000
    assert bloom.memory_bytes() == (bloom.bits + 7) // 8


@pytest.mark.parametrize("error_rate", [0.01, 0.05])
def test_false_positive_rate_matches_the_formula(error_rate: float) -> None:
    capacity = 5_000
    bloom = BloomFilter(capacity, error_rate)
    for item in random_items("m", capacity, seed=2):
        bloom.add(item)
    probes = random_items("p", 40_000, seed=3)
    measured = sum(item in bloom for item in probes) / len(probes)
    expected = false_positive_rate(bloom.bits, bloom.hashes, capacity)
    assert expected == pytest.approx(error_rate, rel=0.05)
    assert measured == pytest.approx(expected, rel=0.25)  # 40k probes: a few sigma of slack
    assert bloom.expected_error_rate == expected
    assert bloom.fill_ratio == pytest.approx(0.5, abs=0.03)  # optimal k fills half the bits


def test_overfilling_raises_the_error_rate() -> None:
    bloom = BloomFilter(capacity=1_000, error_rate=0.01)
    for item in random_items("m", 3_000, seed=4):
        bloom.add(item)
    probes = random_items("p", 20_000, seed=5)
    measured = sum(item in bloom for item in probes) / len(probes)
    assert measured > 0.1
    assert bloom.expected_error_rate > 0.1
    assert bloom.fill_ratio > 0.8


def test_counting_filter_supports_remove() -> None:
    counting = CountingBloomFilter(capacity=1_000, error_rate=0.01)
    items = random_items("m", 500, seed=6)
    for item in items:
        counting.add(item)
    assert all(item in counting for item in items)
    for item in items[:250]:
        counting.remove(item)
    assert all(item in counting for item in items[250:])  # survivors never disappear
    assert sum(item in counting for item in items[:250]) < 25  # removed ones are (mostly) gone
    with pytest.raises(ValidationError):
        counting.remove("never-added")
    assert counting.memory_bytes() == counting.slots


@pytest.mark.parametrize(("capacity", "error_rate"), [(0, 0.01), (-5, 0.01), (10, 0.0), (10, 1.0)])
def test_validation(capacity: int, error_rate: float) -> None:
    with pytest.raises(ValidationError):
        BloomFilter(capacity, error_rate)
    with pytest.raises(ValidationError):
        CountingBloomFilter(capacity, error_rate)


def test_concurrent_adds_equal_sequential_adds() -> None:
    items = random_items("m", 4_000, seed=7)
    sequential = BloomFilter(capacity=4_000, error_rate=0.01)
    for item in items:
        sequential.add(item)
    concurrent = BloomFilter(capacity=4_000, error_rate=0.01)
    chunks = [items[i::8] for i in range(8)]

    def add_all(chunk: list[str]) -> None:
        for item in chunk:
            concurrent.add(item)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_all, chunks))
    assert len(concurrent) == 4_000
    assert concurrent.fill_ratio == sequential.fill_ratio
    assert all(item in concurrent for item in items)
