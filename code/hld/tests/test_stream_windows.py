"""Tests for event-time windows, watermarks and the late-event policy."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.stream_windows import (
    Event,
    Verdict,
    Window,
    WindowAssigner,
    WindowedAggregator,
)


def tumbling(size: float = 60.0, **kwargs: float) -> WindowedAggregator:
    return WindowedAggregator(WindowAssigner(size_seconds=size), **kwargs)


def test_tumbling_assigns_exactly_one_window_on_half_open_boundaries() -> None:
    assigner = WindowAssigner(size_seconds=60.0)
    assert assigner.panes_per_event == 1
    assert assigner.windows_for(0.0) == [Window(0.0, 60.0)]
    assert assigner.windows_for(59.999) == [Window(0.0, 60.0)]
    assert assigner.windows_for(60.0) == [Window(60.0, 120.0)]  # end is exclusive
    assert assigner.windows_for(130.0) == [Window(120.0, 180.0)]
    assert str(assigner.windows_for(0.0)[0]) == "[0, 60)"


def test_sliding_assigns_size_over_slide_windows() -> None:
    assigner = WindowAssigner(size_seconds=300.0, slide_seconds=60.0)
    assert assigner.panes_per_event == 5
    windows = assigner.windows_for(310.0)
    assert windows == [
        Window(60.0, 360.0),
        Window(120.0, 420.0),
        Window(180.0, 480.0),
        Window(240.0, 540.0),
        Window(300.0, 600.0),
    ]
    assert all(w.start <= 310.0 < w.end for w in windows)
    # 5x the panes means 5x the state and 5x the output of the tumbling equivalent
    assert len(windows) == assigner.panes_per_event


def test_the_watermark_follows_the_largest_event_time_not_the_wall_clock() -> None:
    aggregator = tumbling(max_out_of_orderness=5.0)
    assert aggregator.watermark == float("-inf")
    aggregator.ingest(Event("a", 100.0))
    assert aggregator.watermark == 95.0
    aggregator.ingest(Event("a", 50.0))  # out of order: the watermark never moves backwards
    assert aggregator.watermark == 95.0
    aggregator.ingest(Event("a", 200.0))
    assert aggregator.watermark == 195.0


def test_a_window_fires_once_when_the_watermark_passes_its_end() -> None:
    aggregator = tumbling(max_out_of_orderness=5.0)
    for event in (Event("ad-1", 10.0), Event("ad-2", 20.0), Event("ad-1", 55.0)):
        assert aggregator.ingest(event) is Verdict.ON_TIME
    assert aggregator.poll() == []  # watermark 50 is still inside [0, 60)

    assert aggregator.ingest(Event("ad-2", 70.0)) is Verdict.ON_TIME  # watermark 65
    fired = aggregator.poll()
    assert [(r.key, str(r.window), r.total, r.events, r.revision) for r in fired] == [
        ("ad-1", "[0, 60)", 2, 2, 0),
        ("ad-2", "[0, 60)", 1, 1, 0),
    ]
    assert aggregator.poll() == []  # firing is not repeated without new data


def test_a_late_event_inside_the_allowed_lateness_revises_the_window() -> None:
    aggregator = tumbling(max_out_of_orderness=5.0, allowed_lateness=30.0)
    aggregator.ingest_all([Event("ad-1", 10.0), Event("ad-1", 55.0), Event("ad-1", 70.0)])
    first = aggregator.poll()
    assert (first[0].total, first[0].revision, first[0].final) == (2, 0, False)

    assert aggregator.ingest(Event("ad-1", 50.0)) is Verdict.LATE_UPDATE
    revised = aggregator.poll()
    assert (revised[0].total, revised[0].events, revised[0].revision) == (3, 3, 1)
    assert str(revised[0].window) == "[0, 60)"
    assert aggregator.side_output == ()


def test_events_past_the_allowed_lateness_reach_the_side_output_and_state_is_evicted() -> None:
    aggregator = tumbling(max_out_of_orderness=5.0, allowed_lateness=30.0)
    aggregator.ingest_all([Event("ad-1", 10.0), Event("ad-1", 70.0)])
    aggregator.poll()
    assert aggregator.open_windows == 2

    aggregator.ingest(Event("ad-1", 95.0))  # watermark 90 = 60 + 30, so [0, 60) closes
    aggregator.poll()
    assert aggregator.open_windows == 1  # the pane for [0, 60) was evicted

    too_late = Event("ad-1", 45.0)
    assert aggregator.ingest(too_late) is Verdict.DROPPED
    assert aggregator.side_output == (too_late,)
    assert aggregator.open_windows == 1  # no state resurrected for a closed window


def test_a_zero_lateness_pipeline_drops_anything_behind_the_watermark() -> None:
    aggregator = tumbling(size=10.0)  # bound 0, lateness 0: the strictest setting
    aggregator.ingest_all([Event("k", 1.0), Event("k", 25.0)])
    assert aggregator.poll()[0].total == 1
    assert aggregator.ingest(Event("k", 5.0)) is Verdict.DROPPED
    assert len(aggregator.side_output) == 1
    # widen the bound instead and the same event would have been counted
    forgiving = tumbling(size=10.0, max_out_of_orderness=30.0)
    forgiving.ingest_all([Event("k", 1.0), Event("k", 25.0)])
    assert forgiving.poll() == []
    assert forgiving.ingest(Event("k", 5.0)) is Verdict.ON_TIME


@pytest.mark.parametrize(
    ("size", "slide"),
    [(0.0, None), (-1.0, None), (60.0, 0.0), (60.0, -5.0), (60.0, 90.0)],
)
def test_invalid_assigners_are_rejected(size: float, slide: float | None) -> None:
    with pytest.raises(ValidationError):
        WindowAssigner(size_seconds=size, slide_seconds=slide)


def test_negative_lateness_bounds_are_rejected() -> None:
    assigner = WindowAssigner(size_seconds=60.0)
    with pytest.raises(ValidationError):
        WindowedAggregator(assigner, max_out_of_orderness=-1.0)
    with pytest.raises(ValidationError):
        WindowedAggregator(assigner, allowed_lateness=-1.0)


def test_concurrent_ingest_from_many_partitions_loses_no_event() -> None:
    # generous lateness so the racing watermark cannot drop anything: the counts must be exact
    aggregator = WindowedAggregator(
        WindowAssigner(size_seconds=10.0), max_out_of_orderness=0.0, allowed_lateness=3_600.0
    )
    workers, per_worker = 8, 50

    def partition(worker: int) -> list[Verdict]:
        return aggregator.ingest_all(
            Event("ad", event_time=(worker * per_worker + i) % 400 * 0.1) for i in range(per_worker)
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        verdicts = [v for batch in pool.map(partition, range(workers)) for v in batch]

    assert len(verdicts) == workers * per_worker
    assert Verdict.DROPPED not in verdicts
    aggregator.ingest(Event("sentinel", 10_000.0))  # push the watermark past every window
    fired = aggregator.poll()
    counted = sum(r.total for r in fired if r.key == "ad")
    assert counted == workers * per_worker
    assert sum(r.events for r in fired if r.key == "ad") == workers * per_worker
    assert len({r.window for r in fired if r.key == "ad"}) == 4  # 40 s of data, 10 s windows
    assert aggregator.side_output == ()
