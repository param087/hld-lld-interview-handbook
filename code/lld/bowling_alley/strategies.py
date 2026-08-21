"""Two policies: how a card is scored and how a booking is priced."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from common import Money, ValidationError
from lld.bowling_alley.models import (
    PINS,
    Booking,
    Frame,
    FrameScore,
    FrameStatus,
    FrameType,
)


# --8<-- [start:scoring]
class ScoreCalculator(Protocol):
    """Turns a card of frames into a card of scores. Stateless, so it is thread-safe."""

    def score(self, frames: Sequence[Frame]) -> list[FrameScore]: ...


class StandardScoring:
    """Ten-pin scoring: a strike is worth 10 plus the next two balls, a spare 10 plus one.

    The whole card is recomputed from the roll list on every call. That is O(rolls) -
    at most 21 numbers - so there is no incremental cache to invalidate and a bonus
    that arrives three frames later is picked up automatically. A frame whose bonus
    balls have not been thrown yet is reported as ``AWAITING_BONUS`` with the bonus
    counted as zero, which is exactly the provisional total a real scoreboard shows.
    """

    def score(self, frames: Sequence[Frame]) -> list[FrameScore]:
        pins = [roll.pins for frame in frames for roll in frame.rolls]
        card: list[FrameScore] = []
        index = 0
        running = 0
        for frame in frames:
            thrown = len(frame.rolls)
            if thrown == 0:
                card.append(FrameScore(frame.number, FrameType.INCOMPLETE, FrameStatus.EMPTY, 0, 0, running))
                continue
            knocked, bonus, resolved = self._value(frame, pins, index)
            running += knocked + bonus
            card.append(
                FrameScore(
                    number=frame.number,
                    frame_type=frame.frame_type,
                    status=self._status(frame, resolved),
                    pins=knocked,
                    bonus=bonus,
                    running_total=running,
                )
            )
            index += thrown
        return card

    @staticmethod
    def _value(frame: Frame, pins: list[int], index: int) -> tuple[int, int, bool]:
        """Returns (pins knocked in the frame, bonus, whether the bonus is fully thrown)."""
        if frame.is_last:  # the bonus balls are part of the tenth frame itself
            return frame.pins_knocked(), 0, frame.is_complete()
        if frame.rolls[0].pins == PINS:
            extra = pins[index + 1 : index + 3]
            return PINS, sum(extra), len(extra) == 2
        if frame.is_complete() and frame.pins_knocked() == PINS:
            extra = pins[index + 2 : index + 3]
            return PINS, sum(extra), len(extra) == 1
        return frame.pins_knocked(), 0, frame.is_complete()

    @staticmethod
    def _status(frame: Frame, resolved: bool) -> FrameStatus:
        if not frame.is_complete():
            return FrameStatus.IN_PROGRESS
        return FrameStatus.SCORED if resolved else FrameStatus.AWAITING_BONUS


# --8<-- [end:scoring]


# --8<-- [start:pricing]
class PricingStrategy(Protocol):
    """What a booking costs. Money is integer cents, never a float."""

    def quote(self, booking: Booking) -> Money: ...


DEFAULT_PER_GAME = Money.of("6.50")
DEFAULT_SHOE_RENTAL = Money.of("3.00")


class PerGamePricing:
    """The usual house rule: a price per player per game, plus shoe rental."""

    def __init__(
        self,
        per_game: Money = DEFAULT_PER_GAME,
        shoe_rental: Money = DEFAULT_SHOE_RENTAL,
    ) -> None:
        self._per_game = per_game
        self._shoe_rental = shoe_rental

    def quote(self, booking: Booking) -> Money:
        return self._per_game * (len(booking.players) * booking.games) + self._shoe_rental * booking.shoes


class HappyHourPricing:
    """A percentage off any other policy. Composition, so discounts stack instead of subclassing."""

    def __init__(self, base: PricingStrategy, percent_off: int) -> None:
        if not 0 <= percent_off <= 100:
            raise ValidationError("percent_off must be between 0 and 100")
        self._base = base
        self._percent_off = percent_off

    def quote(self, booking: Booking) -> Money:
        full = self._base.quote(booking)
        return Money(full.cents - full.cents * self._percent_off // 100, full.currency)


# --8<-- [end:pricing]
