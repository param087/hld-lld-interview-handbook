"""The two policies an auction house argues about: bid increments and how it closes.

Both are Strategy objects on ``AuctionRules``, so a category with different
increments or a charity auction with a longer anti-snipe window is a
configuration change, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from common import Money, ValidationError
from lld.online_auction.models import Auction


# --8<-- [start:increment]
class IncrementStrategy(Protocol):
    """Given the current price, what is the smallest acceptable next maximum?"""

    def minimum_next(self, current: Money) -> Money: ...


class FixedIncrement:
    """One step for every price. Simple, and wrong for a catalogue spanning 1 to 10 000."""

    def __init__(self, step: Money) -> None:
        if step.cents <= 0:
            raise ValidationError("increment must be positive")
        self._step = step

    def minimum_next(self, current: Money) -> Money:
        return current + self._step


class PercentIncrement:
    """A share of the current price with a floor, for categories with a wide range."""

    def __init__(self, basis_points: int, floor: Money) -> None:
        self._basis_points = basis_points
        self._floor = floor

    def minimum_next(self, current: Money) -> Money:
        step = Money(current.cents * self._basis_points // 10_000, current.currency)
        return current + (step if step > self._floor else self._floor)


# (upper bound in cents, step in cents); the last band has no upper bound.
DEFAULT_BANDS: tuple[tuple[int, int], ...] = (
    (100, 5),
    (500, 25),
    (2_500, 50),
    (10_000, 100),
    (25_000, 250),
    (50_000, 500),
)
DEFAULT_TOP_STEP = 1_000


class TieredIncrement:
    """Bands, the way real marketplaces do it: 5 cents at a dollar, 10 dollars at a thousand."""

    def __init__(
        self, bands: tuple[tuple[int, int], ...] = DEFAULT_BANDS, top_step: int = DEFAULT_TOP_STEP
    ) -> None:
        self._bands = bands
        self._top_step = top_step

    def minimum_next(self, current: Money) -> Money:
        for upper, step in self._bands:
            if current.cents < upper:
                return Money(current.cents + step, current.currency)
        return Money(current.cents + self._top_step, current.currency)


# --8<-- [end:increment]


# --8<-- [start:closing]
class ClosingPolicy(Protocol):
    """Given a bid that has just been accepted, when should the auction now end?"""

    def next_end_time(self, auction: Auction, bid_at: float) -> float: ...


class HardClose:
    """The clock is the clock. Cheap, and the reason sniping works."""

    def next_end_time(self, auction: Auction, bid_at: float) -> float:
        return auction.ends_at


class AntiSnipeExtension:
    """A bid inside the window pushes the end out, up to a bounded number of times.

    The bound matters: without it two determined bidders can extend an auction
    indefinitely, and the auction never settles.
    """

    def __init__(self, window_seconds: float = 120.0, extension_seconds: float = 120.0, max_extensions: int = 5) -> None:
        self._window = window_seconds
        self._extension = extension_seconds
        self._max_extensions = max_extensions

    def next_end_time(self, auction: Auction, bid_at: float) -> float:
        inside_window = auction.ends_at - bid_at <= self._window
        if inside_window and auction.extension_count < self._max_extensions:
            return max(auction.ends_at, bid_at + self._extension)
        return auction.ends_at


@dataclass(frozen=True, slots=True)
class AuctionRules:
    """The pair of policies an auction runs under. Injected, never looked up globally."""

    increment: IncrementStrategy
    closing: ClosingPolicy

    @classmethod
    def default(cls) -> AuctionRules:
        return cls(TieredIncrement(), AntiSnipeExtension())


# --8<-- [end:closing]
