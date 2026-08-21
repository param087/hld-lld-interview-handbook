import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.percentiles import (
    Histogram,
    bimodal_latencies,
    exact_percentile,
    mean_of_percentiles,
    merged_percentile,
)


def fill(samples: list[float], factor: float = 1.25) -> Histogram:
    hist = Histogram.exponential(start=1.0, factor=factor, count=40)
    for value in samples:
        hist.observe(value)
    return hist


def test_exact_percentile_is_nearest_rank() -> None:
    samples = [float(i) for i in range(100, 0, -1)]  # unsorted on purpose
    assert exact_percentile(samples, 50) == 50
    assert exact_percentile(samples, 99) == 99
    assert exact_percentile(samples, 100) == 100
    assert exact_percentile(samples, 0.5) == 1
    assert exact_percentile([7.0], 99.9) == 7.0
    with pytest.raises(ValidationError):
        exact_percentile([], 50)
    for q in (0, -1, 101):
        with pytest.raises(ValidationError):
            exact_percentile(samples, q)


@pytest.mark.parametrize("q", [50, 90, 99, 99.9])
def test_histogram_estimate_lands_in_the_same_bucket_as_the_exact_value(q: float) -> None:
    samples = bimodal_latencies(random.Random(42), 20_000, 10, 400, 0.05)
    hist = fill(samples)
    exact = exact_percentile(samples, q)
    estimate = hist.percentile(q)
    assert exact / 1.25 <= estimate <= exact * 1.25
    assert hist.count == len(samples)
    assert hist.mean == pytest.approx(sum(samples) / len(samples))


def test_merge_equals_one_histogram_over_all_samples() -> None:
    rng = random.Random(7)
    a = [rng.uniform(1, 100) for _ in range(500)]
    b = [rng.uniform(50, 2_000) for _ in range(300)]
    ha, hb, hall = fill(a), fill(b), fill(a + b)
    merged = ha.merge(hb)
    counts, total, total_sum = merged.snapshot()
    all_counts, all_total, all_sum = hall.snapshot()
    assert (counts, total) == (all_counts, all_total)
    assert total_sum == pytest.approx(all_sum)
    assert merged.percentile(99) == hall.percentile(99)
    assert merged_percentile([ha, hb], 99) == hall.percentile(99)
    assert ha.count == 500 and hb.count == 300  # merge copies, it does not mutate
    with pytest.raises(ValidationError):
        ha.merge(Histogram.exponential(1.0, 2.0, 10))


def test_mean_of_percentiles_is_not_the_fleet_percentile() -> None:
    rng = random.Random(42)
    groups = [bimodal_latencies(rng, 30_000, 10, 300, 0.002) for _ in range(3)]
    groups.append(bimodal_latencies(rng, 200, 900, 900, 0.0))  # tiny, fully broken canary
    hists = [fill(group) for group in groups]
    pooled = [value for group in groups for value in group]
    exact = exact_percentile(pooled, 99)
    merged = merged_percentile(hists, 99)
    assert exact / 1.25 <= merged <= exact * 1.25
    assert mean_of_percentiles(hists, 99) > 10 * merged
    assert hists[-1].percentile(99) > 1_000  # visible only when sliced by host


def test_bimodal_mean_describes_no_mode() -> None:
    samples = bimodal_latencies(random.Random(1), 10_000, 10, 400, 0.05)
    mean = sum(samples) / len(samples)
    assert exact_percentile(samples, 50) < 15 < mean < 100 < exact_percentile(samples, 99)


def test_overflow_bucket_reports_the_last_edge_and_interpolation_is_linear() -> None:
    hist = Histogram([1, 2, 4])
    for value in (0.5, 1.0, 3.0, 100.0, 1_000.0):
        hist.observe(value)
    assert hist.snapshot() == ([2, 0, 1, 2], 5, 1104.5)
    assert hist.percentile(20) == 0.5  # rank 1 of 2 in (0, 1]
    assert hist.percentile(60) == 4.0  # rank 3 is the single sample in (2, 4]
    assert hist.percentile(99) == 4.0  # overflow: only "above the last edge" is known


def test_validation_errors() -> None:
    for bad in ([], [0, 1], [2, 1], [1, 1]):
        with pytest.raises(ValidationError):
            Histogram(bad)
    for args in ((0, 2, 3), (1, 1, 3), (1, 2, 0)):
        with pytest.raises(ValidationError):
            Histogram.exponential(*args)
    hist = Histogram.exponential(1, 2, 5)
    with pytest.raises(ValidationError):
        hist.observe(-1)
    with pytest.raises(ValidationError):
        hist.percentile(50)  # empty
    hist.observe(3)
    for q in (0, 101):
        with pytest.raises(ValidationError):
            hist.percentile(q)
    with pytest.raises(ValidationError):
        mean_of_percentiles([], 99)
    with pytest.raises(ValidationError):
        merged_percentile([], 99)
    with pytest.raises(ValidationError):
        bimodal_latencies(random.Random(0), 10, 10, 400, 1.5)


def test_concurrent_observe_counts_every_sample() -> None:
    hist = Histogram.exponential(1, 2, 20)

    def work(worker: int) -> int:
        for i in range(1_000):
            hist.observe((i * 7 + worker) % 700 + 1)
        return worker

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sorted(pool.map(work, range(8))) == list(range(8))
    counts, total, _ = hist.snapshot()
    assert hist.count == total == sum(counts) == 8_000
