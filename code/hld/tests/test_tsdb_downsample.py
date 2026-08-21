"""Tests for the in-memory TSDB: label index, cardinality budget, rollups and alert rules."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.tsdb_downsample import (
    Aggregation,
    AlertRouter,
    AlertRule,
    AlertState,
    Bucket,
    LabelIndex,
    RuleEvaluator,
    Series,
    TimeSeriesDB,
    canonical,
    series_id,
)

T0 = 1_700_000_100.0  # aligned to 60 s and 300 s, like the demo


def _seed(db: TimeSeriesDB, seconds: int = 900, step: int = 10) -> None:
    """Two checkout instances and one cart instance, one sample every ``step`` seconds."""
    for offset in range(0, seconds, step):
        db.write("latency_seconds", {"service": "checkout", "instance": "i-1"}, 0.2, T0 + offset)
        db.write("latency_seconds", {"service": "checkout", "instance": "i-2"}, 0.4, T0 + offset)
        db.write("latency_seconds", {"service": "cart", "instance": "i-3"}, 0.1, T0 + offset)


def test_label_index_intersects_matchers_and_counts_cardinality() -> None:
    index = LabelIndex()
    for instance, service in (("i-1", "checkout"), ("i-2", "checkout"), ("i-3", "cart")):
        labels = canonical({"service": service, "instance": instance})
        index.add(Series(series_id("latency_seconds", labels), "latency_seconds", labels))

    assert len(index.select("latency_seconds", {"service": "checkout"})) == 2
    assert index.select("latency_seconds", {"service": "checkout", "instance": "i-3"}) == []
    assert index.select("latency_seconds", {"service": "cart", "instance": "i-3"}) == [
        'latency_seconds{instance=i-3,service=cart}'
    ]
    assert index.select("no_such_metric", {}) == []
    assert index.label_cardinality("instance") == 3
    assert index.label_cardinality("region") == 0


def test_cardinality_budget_rejects_a_new_series_but_not_a_new_sample() -> None:
    db = TimeSeriesDB(max_series=2)
    db.write("latency_seconds", {"instance": "i-1"}, 0.2, T0)
    db.write("latency_seconds", {"instance": "i-2"}, 0.3, T0)

    with pytest.raises(ValidationError, match="cardinality limit"):
        db.write("latency_seconds", {"instance": "i-3"}, 0.4, T0)

    db.write("latency_seconds", {"instance": "i-1"}, 0.25, T0 + 10)  # existing series still works
    assert db.cardinality == 2
    assert db.sample_count() == 3


def test_out_of_order_sample_is_rejected() -> None:
    db = TimeSeriesDB()
    db.write("cpu_seconds", {"instance": "i-1"}, 1.0, T0 + 60)
    with pytest.raises(ValidationError, match="out-of-order"):
        db.write("cpu_seconds", {"instance": "i-1"}, 1.0, T0 + 30)


def test_write_without_a_timestamp_uses_the_injected_clock() -> None:
    clock = FakeClock(start=T0)
    db = TimeSeriesDB(clock=clock)
    db.write("cpu_seconds", {"instance": "i-1"}, 1.0)
    clock.advance(30)
    db.write("cpu_seconds", {"instance": "i-1"}, 2.0)
    points = db.range_query("cpu_seconds", start=T0, end=T0 + 60, step=60, agg=Aggregation.LAST)
    assert [(p.timestamp, p.value) for p in points] == [(T0, 2.0)]


@pytest.mark.parametrize(
    ("agg", "expected"),
    [
        (Aggregation.AVG, [0.3, 0.3, 0.3]),
        (Aggregation.MAX, [0.4, 0.4, 0.4]),
        (Aggregation.MIN, [0.2, 0.2, 0.2]),
        (Aggregation.COUNT, [60.0, 60.0, 60.0]),
    ],
)
def test_rollup_tier_answers_exactly_what_the_raw_samples_would(
    agg: Aggregation, expected: list[float]
) -> None:
    db = TimeSeriesDB()
    _seed(db)
    query = dict(start=T0, end=T0 + 900, step=300, agg=agg)
    raw = db.range_query("latency_seconds", {"service": "checkout"}, **query)
    assert [round(p.value, 9) for p in raw] == expected

    dropped, written = db.compact(step=300, before=T0 + 900)
    assert (dropped, written) == (270, 9)  # 3 series x 90 samples -> 3 series x 3 buckets
    assert db.sample_count() == 0

    rolled = db.range_query("latency_seconds", {"service": "checkout"}, **query)
    assert [round(p.value, 9) for p in rolled] == expected
    assert [p.timestamp for p in rolled] == [p.timestamp for p in raw]


def test_query_planner_refuses_a_step_no_tier_can_answer() -> None:
    db = TimeSeriesDB()
    _seed(db)
    db.compact(step=300, before=T0 + 900)
    # 60 s resolution is gone: the finest surviving tier is 300 s.
    with pytest.raises(ValidationError, match="no tier stores"):
        db.range_query("latency_seconds", start=T0, end=T0 + 900, step=60)
    # 900 s is a multiple of 300 s, so the 5-minute tier can serve it.
    assert len(db.range_query("latency_seconds", start=T0, end=T0 + 900, step=900)) == 1


def test_buckets_merge_associatively() -> None:
    left = Bucket.of(0.0, [1.0, 3.0])
    right = Bucket.of(300.0, [5.0])
    merged = left.merge(right)
    assert (merged.start, merged.count, merged.total) == (0.0, 3, 9.0)
    assert (merged.minimum, merged.maximum, merged.last) == (1.0, 5.0, 5.0)
    assert right.merge(left) == merged


def test_for_duration_delays_firing_and_recovery_clears_it() -> None:
    db = TimeSeriesDB()
    for offset in range(0, 600, 10):
        db.write("latency_seconds", {"service": "checkout"}, 0.9 if offset < 300 else 0.1, T0 + offset)
    rule = AlertRule(
        name="CheckoutLatencyHigh",
        metric="latency_seconds",
        matchers=canonical({"service": "checkout"}),
        threshold=0.5,
        window_s=60,
        for_s=120.0,
        agg=Aggregation.MAX,
    )
    evaluator = RuleEvaluator(db)
    states = [evaluator.evaluate([rule], T0 + t)[0].state for t in (60, 120, 180, 360)]
    assert states == [
        AlertState.PENDING,  # first breach: the for: timer starts
        AlertState.PENDING,  # 60 s of 120 s elapsed
        AlertState.FIRING,  # held long enough
        AlertState.INACTIVE,  # recovered
    ]


def test_router_deduplicates_repeats_and_resolves() -> None:
    db = TimeSeriesDB()
    for offset in range(0, 900, 10):
        db.write("latency_seconds", {"service": "checkout"}, 0.9 if offset < 600 else 0.1, T0 + offset)
    rule = AlertRule(
        name="CheckoutLatencyHigh",
        metric="latency_seconds",
        matchers=canonical({"service": "checkout"}),
        threshold=0.5,
        window_s=60,
        agg=Aggregation.MAX,
        severity="page",
        group_by=("service",),
    )
    evaluator = RuleEvaluator(db)
    router = AlertRouter({"page": "oncall-payments"}, repeat_interval_s=300.0)

    def dispatch(offset: int) -> list[tuple[str, str]]:
        now = T0 + offset
        return [(n.receiver, n.reason) for n in router.dispatch(evaluator.evaluate([rule], now), now)]

    assert dispatch(60) == [("oncall-payments", "new")]
    assert dispatch(90) == []  # deduplicated inside the repeat interval
    assert dispatch(390) == [("oncall-payments", "repeat")]
    assert dispatch(720) == [("oncall-payments", "resolved")]
    assert dispatch(780) == []  # nothing left to resolve


def test_concurrent_writers_lose_no_samples() -> None:
    db = TimeSeriesDB(max_series=64)
    writers, samples = 8, 200

    def scrape(worker: int) -> None:
        for i in range(samples):
            db.write("scrape_seconds", {"instance": f"i-{worker}"}, float(i), T0 + i)

    with ThreadPoolExecutor(max_workers=writers) as pool:
        list(pool.map(scrape, range(writers)))

    assert db.cardinality == writers
    assert db.sample_count() == writers * samples
    assert db.label_cardinality("instance") == writers
    total = db.range_query("scrape_seconds", start=T0, end=T0 + 300, step=300, agg=Aggregation.COUNT)
    assert [p.value for p in total] == [float(writers * samples)]
