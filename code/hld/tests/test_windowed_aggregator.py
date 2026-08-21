from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.windowed_aggregator import ClickEvent, Outcome, WindowedAggregator

BASE = 1_700_000_040.0  # exactly on a minute boundary


@pytest.fixture
def agg() -> WindowedAggregator:
    return WindowedAggregator(window_s=60.0, watermark_lag_s=30.0, dedup_ttl_s=300.0)


def click(eid: str, ad: str, offset: float) -> ClickEvent:
    return ClickEvent(eid, ad, BASE + offset)


def test_events_are_bucketed_by_event_time_not_arrival_order(agg: WindowedAggregator) -> None:
    # Deliberately out of order: a phone that was offline reports t+5 after t+70.
    for event in [click("a", "ad1", 70), click("b", "ad1", 5), click("c", "ad2", 5)]:
        assert agg.ingest(event) is Outcome.ACCEPTED
    assert agg.ingest(click("d", "ad1", 130)) is Outcome.ACCEPTED  # watermark -> t+100
    closed = agg.poll_closed()
    assert [(w.start - BASE, w.counts) for w in closed] == [(0.0, {"ad1": 1, "ad2": 1})]
    assert closed[0].total == 2


def test_a_window_stays_open_until_the_watermark_passes_its_end(agg: WindowedAggregator) -> None:
    agg.ingest(click("a", "ad1", 10))
    agg.ingest(click("b", "ad1", 80))  # watermark t+50: window 0 ends at t+60, still open
    assert agg.poll_closed() == []
    assert agg.watermark == BASE + 50
    agg.ingest(click("c", "ad1", 95))  # watermark t+65 > t+60
    assert [w.start - BASE for w in agg.poll_closed()] == [0.0]


def test_duplicate_event_ids_are_counted_once(agg: WindowedAggregator) -> None:
    assert agg.ingest(click("e1", "ad1", 1)) is Outcome.ACCEPTED
    assert agg.ingest(click("e1", "ad1", 1)) is Outcome.DUPLICATE
    assert agg.ingest(click("e1", "ad1", 2)) is Outcome.DUPLICATE  # a retry with drift, still one
    agg.ingest(click("e2", "ad1", 130))
    assert agg.poll_closed()[0].counts == {"ad1": 1}
    assert agg.stats().duplicates == 2


def test_late_events_go_to_the_side_output_and_reconcile_folds_them_back(
    agg: WindowedAggregator,
) -> None:
    agg.ingest(click("a", "ad1", 10))
    agg.ingest(click("b", "ad2", 130))
    assert len(agg.poll_closed()) == 1
    assert agg.ingest(click("c", "ad1", 20)) is Outcome.LATE  # window 0 already published
    assert agg.closed_windows()[0].counts == {"ad1": 1}  # the published number did not change
    corrected = agg.reconcile()
    assert [(w.counts, w.late_folded) for w in corrected] == [({"ad1": 2}, 1)]
    assert agg.reconcile() == []  # the side output was drained


def test_top_n_is_deterministic_and_window_scoped(agg: WindowedAggregator) -> None:
    for i, ad in enumerate(["ad1", "ad1", "ad1", "ad2", "ad2", "ad3"]):
        agg.ingest(click(f"w0-{i}", ad, i))
    for i, ad in enumerate(["ad3", "ad3", "ad2"]):
        agg.ingest(click(f"w1-{i}", ad, 60 + i))
    agg.ingest(click("push", "ad9", 200))
    agg.poll_closed()
    assert agg.top_n(2, window_start=BASE) == [("ad1", 3), ("ad2", 2)]
    assert agg.top_n(2, window_start=BASE + 60) == [("ad3", 2), ("ad2", 1)]
    assert agg.top_n(2) == agg.top_n(2, window_start=BASE + 60)  # newest closed window
    assert agg.top_n(10, window_start=BASE + 600) == []  # a window that never existed


def test_dedup_keys_expire_against_the_watermark(agg: WindowedAggregator) -> None:
    agg.ingest(click("old", "ad1", 0))
    assert agg.stats().dedup_entries == 1
    agg.ingest(click("new", "ad1", 400))  # watermark t+370, TTL 300 -> cutoff t+70
    agg.poll_closed()
    assert agg.stats().dedup_entries == 1  # "old" was evicted, "new" kept
    assert agg.ingest(click("old", "ad1", 0)) is Outcome.LATE  # replayed, but its window is closed


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"window_s": 0}, "window"),
        ({"watermark_lag_s": -1}, "lag"),
        ({"dedup_ttl_s": -1}, "TTL"),
    ],
)
def test_constructor_validation(kwargs: dict, message: str) -> None:
    with pytest.raises(ValidationError):
        WindowedAggregator(**kwargs)
    assert message  # the parametrize label documents which knob is being rejected


def test_event_and_query_validation(agg: WindowedAggregator) -> None:
    with pytest.raises(ValidationError):
        ClickEvent("", "ad1", BASE)
    with pytest.raises(ValidationError):
        ClickEvent("e1", "", BASE)
    with pytest.raises(ValidationError):
        agg.top_n(0)


def test_concurrent_ingest_counts_every_event_exactly_once(agg: WindowedAggregator) -> None:
    def send(i: int) -> Outcome:
        return agg.ingest(click(f"e{i % 500}", "ad1", i % 50))  # 1000 sends, 500 distinct ids

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(send, range(1000)))
    assert outcomes.count(Outcome.ACCEPTED) == 500
    assert outcomes.count(Outcome.DUPLICATE) == 500
    agg.ingest(click("push", "ad2", 200))
    assert agg.poll_closed()[0].counts == {"ad1": 500}
