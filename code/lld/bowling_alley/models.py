"""Frames, rolls, lanes and bookings.

``Frame`` is where the tenth-frame rules live, and it is the only place they live:
``remaining_pins`` and ``is_complete`` answer every question the game asks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError

PINS = 10
FRAMES = 10


# --8<-- [start:enums]
class FrameType(StrEnum):
    INCOMPLETE = "incomplete"
    STRIKE = "strike"  # all ten with the first ball
    SPARE = "spare"  # all ten with two balls
    OPEN = "open"  # pins left standing


class FrameStatus(StrEnum):
    """A frame's *score* lifecycle, which is what a scoreboard renders."""

    EMPTY = "empty"
    IN_PROGRESS = "in_progress"
    AWAITING_BONUS = "awaiting_bonus"  # complete, but the total still depends on later rolls
    SCORED = "scored"


class LaneStatus(StrEnum):
    FREE = "free"
    RESERVED = "reserved"
    IN_PLAY = "in_play"
    MAINTENANCE = "maintenance"


class InvalidPinCountError(ValidationError):
    """More pins knocked down than are standing, or a negative count."""


class FrameCompleteError(InvalidStateError):
    """The frame has taken all the rolls it is allowed."""


class LaneUnavailableError(ConflictError):
    """No lane is free, or this lane is not in a state that allows the operation."""


class BookingNotFoundError(NotFoundError):
    """Unknown booking id."""


# --8<-- [end:enums]


# --8<-- [start:frame]
@dataclass(frozen=True, slots=True)
class Roll:
    number: int  # 1-based within the frame
    pins: int


@dataclass(slots=True)
class Frame:
    """One frame of one player's card. The tenth frame is the same class with a flag.

    Keeping the last frame here rather than in a subclass means the *rules* differ by
    a flag while the *interface* does not, so ``Game`` never asks which frame it is on.
    """

    number: int
    is_last: bool = False
    rolls: list[Roll] = field(default_factory=list)

    def pins_knocked(self) -> int:
        return sum(roll.pins for roll in self.rolls)

    def remaining_pins(self) -> int:
        """Pins still standing. In the tenth frame the rack resets after a strike or a spare."""
        if not self.is_last:
            return PINS - self.pins_knocked()
        standing = PINS
        for roll in self.rolls:
            standing -= roll.pins
            if standing == 0:
                standing = PINS
        return standing

    def is_complete(self) -> bool:
        if not self.is_last:
            return len(self.rolls) == 2 or (bool(self.rolls) and self.rolls[0].pins == PINS)
        if len(self.rolls) < 2:
            return False
        earned_bonus = self.rolls[0].pins + self.rolls[1].pins >= PINS
        return len(self.rolls) == 3 if earned_bonus else True

    @property
    def frame_type(self) -> FrameType:
        if not self.is_complete():
            return FrameType.INCOMPLETE
        if self.rolls[0].pins == PINS:
            return FrameType.STRIKE
        if self.rolls[0].pins + self.rolls[1].pins == PINS:
            return FrameType.SPARE
        return FrameType.OPEN

    def add(self, pins: int) -> Roll:
        """Validate against the pins actually standing, then record the roll."""
        if self.is_complete():
            raise FrameCompleteError(f"frame {self.number} is already complete")
        if not 0 <= pins <= self.remaining_pins():
            raise InvalidPinCountError(
                f"frame {self.number}: {pins} pins, but {self.remaining_pins()} are standing"
            )
        roll = Roll(len(self.rolls) + 1, pins)
        self.rolls.append(roll)
        return roll

    def marks(self) -> str:
        """The card notation a scoreboard prints: X for a strike, / for a spare, - for a gutter."""
        out: list[str] = []
        standing, fresh = PINS, True
        for roll in self.rolls:
            if roll.pins == PINS and fresh:
                out.append("X")  # a full rack cleared with one ball
            elif roll.pins == standing:
                out.append("/")  # the rest of the rack cleared: a spare
            elif roll.pins == 0:
                out.append("-")
            else:
                out.append(str(roll.pins))
            standing -= roll.pins
            fresh = standing == 0  # the tenth frame re-racks, and the next ball is a fresh one
            if standing == 0:
                standing = PINS
        return "".join(out)


# --8<-- [end:frame]


# --8<-- [start:scores]
@dataclass(frozen=True, slots=True)
class FrameScore:
    """One cell of the scorecard. ``status`` says whether the total can still move."""

    number: int
    frame_type: FrameType
    status: FrameStatus
    pins: int
    bonus: int
    running_total: int

    @property
    def final(self) -> bool:
        return self.status is FrameStatus.SCORED


@dataclass(frozen=True, slots=True)
class Standing:
    """One row of the live scoreboard."""

    player: str
    frame: int
    total: int
    final: bool
    card: str


# --8<-- [end:scores]


# --8<-- [start:lane]
@dataclass(slots=True)
class Lane:
    """A physical lane. The status transitions are guarded so a lane cannot be double booked."""

    id: str
    status: LaneStatus = LaneStatus.FREE
    booking_id: str | None = None

    def is_free(self) -> bool:
        return self.status is LaneStatus.FREE

    def reserve(self, booking_id: str) -> None:
        if not self.is_free():
            raise LaneUnavailableError(f"lane {self.id} is {self.status}")
        self.status = LaneStatus.RESERVED
        self.booking_id = booking_id

    def start_play(self) -> None:
        if self.status is not LaneStatus.RESERVED:
            raise LaneUnavailableError(f"lane {self.id} is {self.status}, not reserved")
        self.status = LaneStatus.IN_PLAY

    def release(self) -> None:
        if self.status not in (LaneStatus.RESERVED, LaneStatus.IN_PLAY):
            raise LaneUnavailableError(f"lane {self.id} is {self.status}")
        self.status = LaneStatus.FREE
        self.booking_id = None

    def take_out_of_service(self) -> None:
        if self.status is LaneStatus.IN_PLAY:
            raise LaneUnavailableError(f"lane {self.id} has a game on it")
        self.status = LaneStatus.MAINTENANCE
        self.booking_id = None

    def return_to_service(self) -> None:
        if self.status is not LaneStatus.MAINTENANCE:
            raise LaneUnavailableError(f"lane {self.id} is not under maintenance")
        self.status = LaneStatus.FREE


@dataclass(frozen=True, slots=True)
class Booking:
    id: str
    lane_id: str
    players: tuple[str, ...]
    games: int
    shoes: int
    price: Money
    created_at: float


# --8<-- [end:lane]
