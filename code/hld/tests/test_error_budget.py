import math
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.error_budget import (
    DAY,
    HOUR,
    MINUTE,
    BudgetTracker,
    BurnRateRule,
    Slo,
    default_rules,
)

SLO = Slo("availability", target=0.999, window_seconds=30 * DAY)
PER_MINUTE = 60_000  # 1,000 QPS


def tick(tracker: BudgetTracker, clock: FakeClock, minutes: int, error_ratio: float) -> None:
    """Record ``minutes`` one-minute samples, each stamped at the end of its minute."""
    for _ in range(minutes):
        clock.advance(MINUTE)
        bad = round(PER_MINUTE * error_ratio)
        tracker.record(PER_MINUTE - bad, bad)


def test_slo_turns_a_target_into_a_budget() -> None:
    assert SLO.budget_fraction == pytest.approx(0.001)
    assert SLO.budget_seconds == pytest.approx(43.2 * MINUTE)
    assert SLO.budget_events(1_000) == pytest.approx(2_592_000)
    four_nines = Slo("strict", 0.9999, 30 * DAY)
    assert four_nines.budget_seconds == pytest.approx(4.32 * MINUTE)


@pytest.mark.parametrize(
    ("error_ratio", "burn", "hours_left"),
    [(0.001, 1.0, 720), (0.002, 2.0, 360), (0.0144, 14.4, 50), (1.0, 1_000.0, 0.72)],
)
def test_burn_rate_and_time_to_exhaustion(error_ratio: float, burn: float, hours_left: float) -> None:
    assert SLO.burn_rate(error_ratio) == pytest.approx(burn)
    assert SLO.time_to_exhaustion_seconds(burn) == pytest.approx(hours_left * HOUR)
    assert SLO.budget_consumed(burn, SLO.time_to_exhaustion_seconds(burn)) == pytest.approx(1.0)


def test_zero_burn_never_exhausts() -> None:
    assert SLO.time_to_exhaustion_seconds(0) == math.inf
    assert SLO.budget_consumed(0, 30 * DAY) == 0


def test_default_rules_put_the_documented_share_of_budget_at_stake() -> None:
    fast, slow, trickle = default_rules()
    assert fast.budget_at_stake(SLO) == pytest.approx(0.02)
    assert slow.budget_at_stake(SLO) == pytest.approx(0.05)
    assert trickle.budget_at_stake(SLO) == pytest.approx(0.10)
    assert [r.severity for r in (fast, slow, trickle)] == ["page", "page", "ticket"]


def test_tracker_answers_any_trailing_window_and_prunes_the_old_ones() -> None:
    clock = FakeClock(0)
    tracker = BudgetTracker(SLO, clock)
    tracker.record(good=990, bad=10)  # 1 % errors at t=0
    clock.advance(HOUR)
    tracker.record(good=1_000, bad=0)
    assert tracker.error_ratio(5 * MINUTE) == 0.0
    assert tracker.error_ratio(2 * HOUR) == pytest.approx(10 / 2_000)
    assert tracker.burn_rate(2 * HOUR) == pytest.approx(5.0)
    assert tracker.totals() == (1_990, 10)
    clock.advance(30 * DAY)  # everything recorded so far is older than the window now
    tracker.record(1, 0)
    assert tracker.totals() == (1, 0)
    assert tracker.error_ratio() == 0.0


def test_budget_remaining_is_measured_against_the_planned_traffic() -> None:
    slo = Slo("small", target=0.9, window_seconds=1_000)  # budget: 100 bad events at 1 QPS
    tracker = BudgetTracker(slo, FakeClock(0))
    tracker.record(good=950, bad=50)
    assert tracker.budget_remaining(qps=1) == pytest.approx(0.5)
    tracker.record(good=0, bad=60)
    assert tracker.budget_remaining(qps=1) == pytest.approx(-0.1)
    with pytest.raises(ValidationError):
        tracker.budget_remaining(qps=0)


def test_multiwindow_alert_fires_once_enough_budget_is_at_stake_and_clears_fast() -> None:
    clock = FakeClock(0)
    tracker = BudgetTracker(SLO, clock)
    rules = default_rules()
    tick(tracker, clock, 3 * DAY // MINUTE, 0.0005)  # steady state, burn 0.5x
    assert tracker.evaluate(rules) == []
    fired_at = None
    for minute in range(1, 61):
        tick(tracker, clock, 1, 0.02)  # burn 20x
        if tracker.evaluate(rules):
            fired_at = minute
            break
    # the 1 h window reaches 14.4x once ~43 of its 60 minutes are at 2 %: 0.02k + 0.0005(60-k) >= 0.864
    assert fired_at is not None and 42 <= fired_at <= 44
    (alert,) = tracker.evaluate(rules)
    assert (alert.rule, alert.severity) == ("fast burn", "page")
    assert alert.long_burn_rate >= 14.4 and alert.short_burn_rate == pytest.approx(20.0)
    tick(tracker, clock, 5, 0.0005)  # rollback: the short window resets within five minutes
    assert tracker.burn_rate(HOUR) > 14.4  # the long window is still hot...
    assert tracker.evaluate(rules) == []  # ...but the alert has cleared


def test_validation_errors() -> None:
    for target in (0, 1, 1.5, -0.1):
        with pytest.raises(ValidationError):
            Slo("bad", target, 30 * DAY)
    with pytest.raises(ValidationError):
        Slo("bad", 0.99, 0)
    with pytest.raises(ValidationError):
        SLO.burn_rate(2.0)
    with pytest.raises(ValidationError):
        SLO.budget_events(-1)
    with pytest.raises(ValidationError):
        SLO.time_to_exhaustion_seconds(-1)
    with pytest.raises(ValidationError):
        BurnRateRule("r", "email", HOUR, 5 * MINUTE, 14.4)
    with pytest.raises(ValidationError):
        BurnRateRule("r", "page", HOUR, HOUR, 14.4)
    with pytest.raises(ValidationError):
        BurnRateRule("r", "page", HOUR, 5 * MINUTE, 0)
    tracker = BudgetTracker(SLO, FakeClock(0))
    with pytest.raises(ValidationError):
        tracker.record(-1, 0)
    for window in (0, 31 * DAY):
        with pytest.raises(ValidationError):
            tracker.totals(window)


def test_concurrent_records_are_all_counted() -> None:
    tracker = BudgetTracker(SLO, FakeClock(0))

    def work(worker: int) -> int:
        for _ in range(500):
            tracker.record(good=9, bad=1)
        return worker

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sorted(pool.map(work, range(8))) == list(range(8))
    assert tracker.totals() == (36_000, 4_000)
    assert tracker.error_ratio() == pytest.approx(0.1)
    assert tracker.burn_rate() == pytest.approx(100.0)
