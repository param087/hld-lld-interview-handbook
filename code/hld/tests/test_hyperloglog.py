import math
import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.hyperloglog import HyperLogLog, alpha


def random_items(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return [f"u:{rng.getrandbits(64):016x}" for _ in range(n)]


def test_error_bound_and_memory_follow_the_precision() -> None:
    for precision in (4, 10, 14, 18):
        hll = HyperLogLog(precision)
        assert hll.registers == 2**precision
        assert hll.memory_bytes() == 2**precision
        assert hll.error_bound == pytest.approx(1.04 / math.sqrt(2**precision))
    assert HyperLogLog(14).error_bound == pytest.approx(0.0081, abs=0.0001)
    assert alpha(16) == 0.673 and alpha(64) == 0.709
    assert alpha(16_384) == pytest.approx(0.7213 / (1 + 1.079 / 16_384))


@pytest.mark.parametrize(("precision", "distinct"), [(10, 50_000), (12, 50_000), (14, 50_000)])
def test_estimate_is_within_three_standard_errors(precision: int, distinct: int) -> None:
    hll = HyperLogLog(precision)
    for item in random_items(distinct, seed=precision):
        hll.add(item)
    estimate = hll.count()
    assert abs(estimate - distinct) / distinct <= 3 * hll.error_bound


def test_duplicates_do_not_change_the_estimate() -> None:
    once, thrice = HyperLogLog(12), HyperLogLog(12)
    items = random_items(20_000, seed=21)
    for item in items:
        once.add(item)
        for _ in range(3):
            thrice.add(item)
    assert once.count() == thrice.count()
    assert abs(once.count() - 20_000) / 20_000 <= 3 * once.error_bound


@pytest.mark.parametrize("distinct", [0, 1, 10, 100, 1_000])
def test_small_cardinalities_use_linear_counting(distinct: int) -> None:
    hll = HyperLogLog(14)
    for i in range(distinct):
        hll.add(f"s:{i}")
    estimate = hll.count()
    if distinct <= 10:
        assert estimate == distinct
    else:
        assert abs(estimate - distinct) <= 0.05 * distinct


def test_merge_estimates_the_union() -> None:
    a, b, union = HyperLogLog(12), HyperLogLog(12), HyperLogLog(12)
    for i in range(30_000):
        a.add(f"u:{i}")
        union.add(f"u:{i}")
    for i in range(20_000, 50_000):
        b.add(f"u:{i}")
        union.add(f"u:{i}")
    a.merge(b)
    assert a.count() == union.count()
    assert abs(a.count() - 50_000) / 50_000 <= 3 * a.error_bound
    before = a.count()
    a.merge(a)
    a.merge(b)  # merging is idempotent
    assert a.count() == before
    with pytest.raises(ValidationError):
        a.merge(HyperLogLog(10))


@pytest.mark.parametrize("precision", [3, 19])
def test_validation(precision: int) -> None:
    with pytest.raises(ValidationError):
        HyperLogLog(precision)


def test_concurrent_adds_equal_sequential_adds() -> None:
    items = random_items(40_000, seed=23)
    sequential = HyperLogLog(12)
    for item in items:
        sequential.add(item)
    concurrent = HyperLogLog(12)
    chunks = [items[i::8] for i in range(8)]

    def add_all(chunk: list[str]) -> None:
        for item in chunk:
            concurrent.add(item)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_all, chunks))
    assert concurrent.count() == sequential.count()
