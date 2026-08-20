"""Strategy: interchangeable algorithms behind one interface.

The running example is pricing a parking stay. ``ExitGate`` (the Context) owns
*a* ``PricingStrategy`` without knowing which one it has; ``HourlyPricing``,
``FlatRatePricing`` and ``DailyCapPricing`` are interchangeable because they
share a single method. The second half of the module restates the same rules
as plain callables, the Pythonic form when the interface has one method.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from operator import itemgetter
from typing import Protocol, runtime_checkable

from common import Money, ValidationError

MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR
DEFAULT_GRACE_MINUTES = 15
DEFAULT_HOURLY_RATE = Money.of("3.00")
DEFAULT_FLAT_RATE = Money.of("10.00")
DEFAULT_DAILY_CAP = Money.of("20.00")


# --8<-- [start:strategy]
@runtime_checkable
class PricingStrategy(Protocol):
    """The Strategy interface: one method, nothing about *which* rule is in force.

    A ``Protocol`` rather than an ``ABC``: the concrete rules never inherit from
    it, they qualify by having a matching ``price`` method (structural typing).
    """

    def price(self, minutes: int) -> Money: ...


@dataclass(frozen=True, slots=True)
class HourlyPricing:
    """Per started hour after a free grace period (the weekday rule)."""

    rate_per_hour: Money = DEFAULT_HOURLY_RATE
    grace_minutes: int = DEFAULT_GRACE_MINUTES

    def price(self, minutes: int) -> Money:
        if minutes <= self.grace_minutes:
            return Money(0, self.rate_per_hour.currency)
        return self.rate_per_hour * math.ceil(minutes / MINUTES_PER_HOUR)


@dataclass(frozen=True, slots=True)
class FlatRatePricing:
    """One price per visit, however long (the event-night rule)."""

    rate: Money = DEFAULT_FLAT_RATE

    def price(self, minutes: int) -> Money:
        return self.rate


@dataclass(frozen=True, slots=True)
class DailyCapPricing:
    """Hourly, but no single day costs more than the cap (the airport rule).

    Composes ``HourlyPricing`` instead of subclassing it: the cap is a rule
    wrapped *around* hourly pricing, so any hourly configuration can be capped.
    """

    hourly: HourlyPricing = field(default_factory=HourlyPricing)
    cap_per_day: Money = DEFAULT_DAILY_CAP

    def price(self, minutes: int) -> Money:
        full_days, remainder = divmod(minutes, MINUTES_PER_DAY)
        return self.cap_per_day * full_days + min(self.hourly.price(remainder), self.cap_per_day)


# --8<-- [end:strategy]


# --8<-- [start:context]
class ExitGate:
    """The Context: holds a reference to *a* rule and delegates the arithmetic to it.

    The gate validates the input once, so no rule repeats the check, and exposes
    the rule as a property so an operator can switch from the weekday rule to the
    event rule while cars are queuing. Swapping is one attribute assignment; the
    rules carry no mutable state, so no lock is involved.
    """

    def __init__(self, pricing: PricingStrategy) -> None:
        self._pricing = pricing

    @property
    def pricing(self) -> PricingStrategy:
        return self._pricing

    @pricing.setter
    def pricing(self, pricing: PricingStrategy) -> None:
        self._pricing = pricing

    def quote(self, minutes: int) -> Money:
        if minutes < 0:
            raise ValidationError("a stay cannot be negative")
        return self._pricing.price(minutes)


# --8<-- [end:context]


# --8<-- [start:functional]
# A Strategy with one method is a function: ``Callable`` is its whole interface.
type PricingRule = Callable[[int], Money]


def hourly(rate: Money = DEFAULT_HOURLY_RATE, grace_minutes: int = DEFAULT_GRACE_MINUTES) -> PricingRule:
    """A closure: the configuration is captured once, the returned function is the rule."""

    def rule(minutes: int) -> Money:
        if minutes <= grace_minutes:
            return Money(0, rate.currency)
        return rate * math.ceil(minutes / MINUTES_PER_HOUR)

    return rule


def flat(rate: Money = DEFAULT_FLAT_RATE) -> PricingRule:
    return lambda _minutes: rate


def daily_cap(inner: PricingRule, cap: Money = DEFAULT_DAILY_CAP) -> PricingRule:
    """Wraps any rule. A bound method such as ``HourlyPricing().price`` qualifies too."""

    def rule(minutes: int) -> Money:
        full_days, remainder = divmod(minutes, MINUTES_PER_DAY)
        return cap * full_days + min(inner(remainder), cap)

    return rule


def rules_by_name() -> dict[str, PricingRule]:
    """Dict dispatch: the mapping replaces both the if/elif ladder and a factory class."""
    return {
        "hourly": hourly(),
        "flat": flat(),
        "daily_cap": daily_cap(hourly()),
    }


def ranked(rules: Mapping[str, PricingRule], minutes: int) -> list[tuple[str, Money]]:
    """``sorted(key=)`` is the Strategy you already use: the key function is the ordering rule."""
    quotes = [(name, rule(minutes)) for name, rule in rules.items()]
    return sorted(quotes, key=itemgetter(1))


# --8<-- [end:functional]


def main() -> None:
    stays = {"2h35m": 2 * MINUTES_PER_HOUR + 35, "26h00m": 26 * MINUTES_PER_HOUR}
    rules: list[PricingStrategy] = [HourlyPricing(), FlatRatePricing(), DailyCapPricing()]
    for label, minutes in stays.items():
        print(f"--- a {label} stay, priced by each rule ---")
        for rule in rules:
            print(f"{type(rule).__name__:>16}: {rule.price(minutes)}")

    gate = ExitGate(HourlyPricing())
    print("--- event night: the same gate switches rules at runtime ---")
    print(f"weekday rule: 2h35m -> {gate.quote(stays['2h35m'])}")
    gate.pricing = FlatRatePricing()
    print(f"event rule:   2h35m -> {gate.quote(stays['2h35m'])}")

    print("--- functional variant: the rules as callables, ranked with sorted(key=) ---")
    for name, fee in ranked(rules_by_name(), stays["26h00m"]):
        print(f"{name:>10}: {fee}")

    try:
        gate.quote(-5)
    except ValidationError as exc:
        print(f"rejected: {exc}")


if __name__ == "__main__":
    main()
