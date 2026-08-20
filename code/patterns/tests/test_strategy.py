"""Strategy: the gate delegates, the rules are interchangeable, and plain functions qualify too."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from common import Money, ValidationError
from patterns.strategy import (
    DailyCapPricing,
    ExitGate,
    FlatRatePricing,
    HourlyPricing,
    PricingStrategy,
    daily_cap,
    ranked,
    rules_by_name,
)

HOUR = 60
DAY = 24 * HOUR


@pytest.mark.parametrize(
    ("strategy", "minutes", "expected"),
    [
        (HourlyPricing(), 15, "0.00"),  # inside the grace period
        (HourlyPricing(), 16, "3.00"),  # one minute past it starts an hour
        (HourlyPricing(), 2 * HOUR + 35, "9.00"),  # started hours round up
        (FlatRatePricing(), 0, "10.00"),
        (FlatRatePricing(), 26 * HOUR, "10.00"),
        (DailyCapPricing(), 10 * HOUR, "20.00"),  # 30.00 hourly, capped
        (DailyCapPricing(), DAY, "20.00"),  # exactly one day, nothing left over
        (DailyCapPricing(), 26 * HOUR, "26.00"),  # one capped day plus two started hours
    ],
)
def test_each_rule_prices_the_same_stay_its_own_way(
    strategy: PricingStrategy, minutes: int, expected: str
) -> None:
    assert ExitGate(strategy).quote(minutes) == Money.of(expected)


def test_gate_swaps_rules_at_runtime_without_touching_them() -> None:
    weekday, event = HourlyPricing(), FlatRatePricing()
    gate = ExitGate(weekday)
    assert gate.quote(2 * HOUR + 35) == Money.of("9.00")
    gate.pricing = event
    assert gate.quote(2 * HOUR + 35) == Money.of("10.00")
    assert gate.pricing is event
    assert weekday == HourlyPricing()  # rules are immutable values; a swap never mutates one


def test_gate_validates_before_delegating_and_a_test_double_needs_no_base_class() -> None:
    class RecordingStrategy:
        def __init__(self) -> None:
            self.seen: list[int] = []

        def price(self, minutes: int) -> Money:
            self.seen.append(minutes)
            return Money(0)

    spy = RecordingStrategy()
    gate = ExitGate(spy)
    with pytest.raises(ValidationError):
        gate.quote(-1)
    assert spy.seen == []  # rejected before the rule was consulted
    assert gate.quote(0) == Money(0) and spy.seen == [0]


def test_protocol_is_satisfied_by_shape_not_by_inheritance() -> None:
    for strategy in (HourlyPricing(), FlatRatePricing(), DailyCapPricing()):
        assert isinstance(strategy, PricingStrategy)
        assert PricingStrategy not in type(strategy).__mro__
    assert not isinstance(object(), PricingStrategy)


@pytest.mark.parametrize("minutes", [0, 15, 16, 2 * HOUR + 35, DAY, 26 * HOUR])
def test_functional_rules_agree_with_the_classes(minutes: int) -> None:
    rules = rules_by_name()
    assert rules["hourly"](minutes) == HourlyPricing().price(minutes)
    assert rules["flat"](minutes) == FlatRatePricing().price(minutes)
    assert rules["daily_cap"](minutes) == DailyCapPricing().price(minutes)


def test_bound_method_is_a_rule_and_sorted_key_ranks_the_quotes() -> None:
    capped = daily_cap(HourlyPricing().price, cap=Money.of("20.00"))
    assert capped(26 * HOUR) == DailyCapPricing().price(26 * HOUR)
    order = [name for name, _ in ranked(rules_by_name(), 26 * HOUR)]
    assert order == ["flat", "daily_cap", "hourly"]


def test_one_frozen_rule_is_shared_safely_by_many_threads() -> None:
    gate = ExitGate(DailyCapPricing())
    with ThreadPoolExecutor(max_workers=8) as pool:
        fees = list(pool.map(gate.quote, [26 * HOUR] * 200))
    assert fees == [Money.of("26.00")] * 200
