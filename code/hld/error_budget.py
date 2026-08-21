"""SLIs, SLOs, error budgets and multi-window burn-rate alerts.

What the module demonstrates, in the order an interviewer asks about it:

* ``Slo`` turns "99.9 % over 30 days" into a budget: the share of events allowed to fail, the
  minutes of total outage that buys, and the failed requests it allows at a given QPS.
* ``burn_rate`` is observed error ratio divided by budgeted error ratio: 1.0 spends the budget
  exactly at the end of the window; 14.4 spends 2 % of a 30-day budget in one hour.
* ``BurnRateRule`` is one row of the multi-window, multi-burn-rate policy: a long window that
  makes the alert significant and a short window that makes it reset quickly; it fires only
  while both burn above the threshold.
* ``BudgetTracker`` ingests good/bad counts stamped by an injected clock, answers the burn rate
  over any trailing window and evaluates the rules: the core of an SLO-based alerting loop.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from common import Clock, FakeClock, SystemClock, ValidationError

MINUTE = 60
HOUR = 3_600
DAY = 86_400


# --8<-- [start:slo]
@dataclass(frozen=True, slots=True)
class Slo:
    """An objective on an SLI ratio (good events / all events) over a rolling window."""

    name: str
    target: float  # 0.999 for "99.9 %"
    window_seconds: int  # 30 days is the usual choice

    def __post_init__(self) -> None:
        if not 0 < self.target < 1:
            raise ValidationError("target must be a fraction strictly between 0 and 1")
        if self.window_seconds <= 0:
            raise ValidationError("window must be positive")

    @property
    def budget_fraction(self) -> float:
        """Share of events allowed to fail: 1 - target, so 0.001 for 99.9 %."""
        return 1.0 - self.target

    @property
    def budget_seconds(self) -> float:
        """Seconds of *total* outage the window allows: 30 d x 0.001 = 2,592 s = 43.2 min."""
        return self.window_seconds * self.budget_fraction

    def budget_events(self, qps: float) -> float:
        """Failed requests the window allows at a steady rate: 1,000 QPS -> 2.59 M."""
        if qps < 0:
            raise ValidationError("qps must be non-negative")
        return qps * self.window_seconds * self.budget_fraction

    def burn_rate(self, error_ratio: float) -> float:
        """How fast the budget is being spent: 1.0 exhausts it exactly at the end of the window."""
        if not 0 <= error_ratio <= 1:
            raise ValidationError("error_ratio must be in [0, 1]")
        return error_ratio / self.budget_fraction

    def time_to_exhaustion_seconds(self, burn_rate: float) -> float:
        """At a constant burn rate the budget lasts window / burn_rate seconds (inf at 0)."""
        if burn_rate < 0:
            raise ValidationError("burn_rate must be non-negative")
        return math.inf if burn_rate == 0 else self.window_seconds / burn_rate

    def budget_consumed(self, burn_rate: float, seconds: float) -> float:
        """Share of the budget a burn rate spends in ``seconds``: 14.4 x 1 h / 720 h = 2 %."""
        if burn_rate < 0 or seconds < 0:
            raise ValidationError("burn_rate and seconds must be non-negative")
        return burn_rate * seconds / self.window_seconds


# --8<-- [end:slo]


# --8<-- [start:rules]
@dataclass(frozen=True, slots=True)
class BurnRateRule:
    """Fire while both the long and the short window burn at or above ``threshold``.

    The long window decides how much budget must be at stake before anyone is woken; the short
    window (1/12 of the long one is the usual ratio) makes the alert clear minutes after the
    fix instead of waiting for the long window to drain.
    """

    name: str
    severity: str  # "page" or "ticket"
    long_window_seconds: int
    short_window_seconds: int
    threshold: float

    def __post_init__(self) -> None:
        if self.severity not in ("page", "ticket"):
            raise ValidationError("severity must be 'page' or 'ticket'")
        if not 0 < self.short_window_seconds < self.long_window_seconds:
            raise ValidationError("short window must be positive and shorter than the long one")
        if self.threshold <= 0:
            raise ValidationError("threshold must be positive")

    def budget_at_stake(self, slo: Slo) -> float:
        """Share of the budget gone by the time this rule fires, if the burn started from zero."""
        return slo.budget_consumed(self.threshold, self.long_window_seconds)


def default_rules() -> list[BurnRateRule]:
    """The standard policy for a 30-day window: 2 % of the budget in 1 h or 5 % in 6 h pages;
    10 % in 3 days opens a ticket."""
    return [
        BurnRateRule("fast burn", "page", HOUR, 5 * MINUTE, 14.4),
        BurnRateRule("slow burn", "page", 6 * HOUR, 30 * MINUTE, 6.0),
        BurnRateRule("trickle", "ticket", 3 * DAY, 6 * HOUR, 1.0),
    ]


@dataclass(frozen=True, slots=True)
class Alert:
    rule: str
    severity: str
    long_burn_rate: float
    short_burn_rate: float


# --8<-- [end:rules]


# --8<-- [start:tracker]
class BudgetTracker:
    """Good/bad event counts for one SLO, queryable over any trailing window.

    ``_lock`` guards ``_events``, a time-ordered deque of ``(timestamp, good, bad)`` pruned to
    the SLO window on every write. Request threads record; the alert evaluator reads.
    """

    def __init__(self, slo: Slo, clock: Clock | None = None) -> None:
        self._slo = slo
        self._clock = clock or SystemClock()
        self._events: deque[tuple[float, int, int]] = deque()
        self._lock = threading.Lock()

    @property
    def slo(self) -> Slo:
        return self._slo

    def record(self, good: int, bad: int) -> None:
        if good < 0 or bad < 0:
            raise ValidationError("counts must be non-negative")
        now = self._clock.now()
        with self._lock:
            self._events.append((now, good, bad))
            cutoff = now - self._slo.window_seconds
            while self._events and self._events[0][0] <= cutoff:
                self._events.popleft()

    def totals(self, window_seconds: int | None = None) -> tuple[int, int]:
        """``(good, bad)`` in the trailing window (default: the whole SLO window)."""
        window = self._slo.window_seconds if window_seconds is None else window_seconds
        if not 0 < window <= self._slo.window_seconds:
            raise ValidationError("window must be positive and within the SLO window")
        cutoff = self._clock.now() - window
        good = bad = 0
        with self._lock:
            for ts, g, b in reversed(self._events):  # newest first; stop at the cutoff
                if ts <= cutoff:
                    break
                good += g
                bad += b
        return good, bad

    def error_ratio(self, window_seconds: int | None = None) -> float:
        good, bad = self.totals(window_seconds)
        return bad / (good + bad) if good + bad else 0.0

    def burn_rate(self, window_seconds: int | None = None) -> float:
        return self._slo.burn_rate(self.error_ratio(window_seconds))

    def budget_remaining(self, qps: float) -> float:
        """Share of the window's budget still unspent at a planned rate (negative once breached)."""
        _, bad = self.totals()
        allowed = self._slo.budget_events(qps)
        if allowed == 0:
            raise ValidationError("a zero-QPS budget has no events to spend")
        return 1.0 - bad / allowed

    def evaluate(self, rules: Iterable[BurnRateRule]) -> list[Alert]:
        """Every rule whose long *and* short windows burn at or above its threshold."""
        alerts: list[Alert] = []
        for rule in rules:
            long_burn = self.burn_rate(rule.long_window_seconds)
            short_burn = self.burn_rate(rule.short_window_seconds)
            if long_burn >= rule.threshold and short_burn >= rule.threshold:
                alerts.append(Alert(rule.name, rule.severity, long_burn, short_burn))
        return alerts


# --8<-- [end:tracker]


def _duration(seconds: float) -> str:
    if seconds >= DAY and seconds % DAY == 0:
        return f"{seconds / DAY:g} d"
    if seconds >= HOUR:
        return f"{seconds / HOUR:g} h"
    return f"{seconds / MINUTE:g} min"


def main() -> None:
    slo = Slo("checkout availability", target=0.999, window_seconds=30 * DAY)
    qps = 1_000
    print(f"SLO: {slo.name} {slo.target:.1%} over {slo.window_seconds // DAY} days")
    print(
        f"  budget: {slo.budget_fraction:.1%} of requests = {slo.budget_seconds / MINUTE:.1f} min "
        f"of total outage; at {qps:,} QPS that is {slo.budget_events(qps):,.0f} failed requests"
    )
    ladder = ", ".join(
        f"{rate:g}x {_duration(slo.time_to_exhaustion_seconds(rate))}"
        for rate in (1, 2, 6, 14.4, 36, 1000)
    )
    print(f"  burn rate -> budget gone in: {ladder} (1000x = total outage)")
    rules = default_rules()
    for rule in rules:
        print(
            f"  {rule.severity:<6} {rule.name:<9} long {_duration(rule.long_window_seconds):>6} / "
            f"short {_duration(rule.short_window_seconds):>6} / burn >= {rule.threshold:<4g} "
            f"-> {rule.budget_at_stake(slo):.0%} of the budget at stake"
        )

    clock = FakeClock(start=0.0)
    tracker = BudgetTracker(slo, clock)
    per_minute = qps * MINUTE

    def tick(error_ratio: float) -> None:
        """One minute of traffic, recorded at the end of the minute it describes."""
        clock.advance(MINUTE)
        bad = round(per_minute * error_ratio)
        tracker.record(per_minute - bad, bad)

    for _ in range(3 * DAY // MINUTE):  # three steady days so every window has history
        tick(0.0005)
    print(f"timeline at {qps:,} QPS after 3 steady days at 0.05% errors (burn 0.5x):")
    minute = 0
    active: set[str] = set()
    for minutes, ratio, label in [(60, 0.0005, "steady"), (50, 0.02, "bad deploy"), (60, 0.0005, "rolled back")]:
        print(f"  t={minute:>3} min  {label}: error ratio {ratio:.2%}, burn {slo.burn_rate(ratio):g}x")
        for _ in range(minutes):
            tick(ratio)
            minute += 1
            firing = {alert.rule for alert in tracker.evaluate(rules)}
            for name in sorted(firing - active):
                print(
                    f"  t={minute:>3} min  PAGE  {name}: 1 h burn {tracker.burn_rate(HOUR):.1f}x, "
                    f"5 min burn {tracker.burn_rate(5 * MINUTE):.1f}x"
                )
            for name in sorted(active - firing):
                print(
                    f"  t={minute:>3} min  CLEAR {name}: 1 h burn {tracker.burn_rate(HOUR):.1f}x, "
                    f"5 min burn {tracker.burn_rate(5 * MINUTE):.1f}x"
                )
            active = firing
    _, bad = tracker.totals()
    incident = slo.budget_consumed(20, 50 * MINUTE)
    print(
        f"  budget used so far: {bad:,} failed requests = {1 - tracker.budget_remaining(qps):.1%}; "
        f"the 50 bad minutes alone burned {incident:.1%} "
        f"(= {incident * slo.budget_seconds / MINUTE:.1f} of the 43.2 min)"
    )


if __name__ == "__main__":
    main()
