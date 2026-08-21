"""How long a green lasts (Strategy).

A strategy sees one immutable `PhaseDemand` and returns a number of ticks. It
cannot reach the lights, the ring or the clock, and whatever it returns is
clamped by the phase's own bounds before it is used - so a broken strategy can
make the intersection inefficient but never unsafe.
"""

from __future__ import annotations

from typing import Protocol

from lld.traffic_signal.models import PhaseDemand


# --8<-- [start:timing]
class TimingStrategy(Protocol):
    name: str

    def green_ticks(self, demand: PhaseDemand) -> int: ...


class FixedTiming:
    """The same green for every phase, every cycle. What most intersections still run."""

    name = "fixed"

    def __init__(self, green: int = 10) -> None:
        if green < 1:
            raise ValueError("green must be at least one tick")
        self._green = green

    def green_ticks(self, demand: PhaseDemand) -> int:
        return self._green


class AdaptiveTiming:
    """A base green plus one tick for every `per_tick` vehicles the loops report.

    The clamp lives in the controller, so this can be tuned aggressively: the
    worst it can do is ask for a green the phase will refuse to give.
    """

    name = "adaptive"

    def __init__(self, base: int = 6, per_tick: int = 2) -> None:
        if base < 1 or per_tick < 1:
            raise ValueError("base and per_tick must be positive")
        self._base = base
        self._per_tick = per_tick

    def green_ticks(self, demand: PhaseDemand) -> int:
        return self._base + demand.waiting_vehicles // self._per_tick


# --8<-- [end:timing]
