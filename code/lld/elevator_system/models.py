"""Enums, button presses as commands, and the small entities of an elevator bank.

Everything that needs a lock (a car, the controller) lives in ``services.py``;
the dispatch policies live in ``strategies.py``. Nothing here imports either, so
the request objects stay free of service dependencies via the ``RequestSink``
protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from common import ConflictError, InvalidStateError, ValidationError


# --8<-- [start:enums]
class Direction(StrEnum):
    UP = "up"
    DOWN = "down"
    IDLE = "idle"

    def opposite(self) -> Direction:
        if self is Direction.UP:
            return Direction.DOWN
        if self is Direction.DOWN:
            return Direction.UP
        return Direction.IDLE

    def arrow(self) -> str:
        return {Direction.UP: "^", Direction.DOWN: "v", Direction.IDLE: "-"}[self]


class ElevatorState(StrEnum):
    """The five statuses a car can be in. Every tick moves the car through this set."""

    IDLE = "idle"
    MOVING_UP = "moving_up"
    MOVING_DOWN = "moving_down"
    DOOR_OPEN = "door_open"
    MAINTENANCE = "maintenance"


class DoorState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    OBSTRUCTED = "obstructed"


# --8<-- [end:enums]


# --8<-- [start:errors]
class FloorOutOfRangeError(ValidationError):
    """The requested floor does not exist in this building."""


class NoCarAvailableError(ConflictError):
    """Every car is in maintenance, so the call cannot be assigned to anyone."""


class CarUnavailableError(InvalidStateError):
    """The car is out of service and refuses stops, boarding or movement."""


class DoorObstructedError(InvalidStateError):
    """A door operation is illegal for the door's current state."""


class CapacityExceededError(ConflictError):
    """One more passenger would put the car over its rated load."""


# --8<-- [end:errors]


# --8<-- [start:requests]
class RequestSink(Protocol):
    """The two controller methods a request needs. Keeps models free of service imports."""

    def assign_hall_call(self, request: HallRequest) -> str: ...

    def assign_cabin_call(self, request: CabinRequest) -> str: ...


class Request(Protocol):
    """Command: a button press as an object the controller can log, queue and replay."""

    requested_at: float

    def apply(self, sink: RequestSink) -> str: ...

    def label(self) -> str: ...


@dataclass(frozen=True, slots=True)
class HallRequest:
    """A landing button: "someone on floor 5 wants to go up"."""

    floor: int
    direction: Direction
    requested_at: float
    destination: int | None = None  # only a destination-dispatch lobby knows this

    def key(self) -> tuple[int, Direction]:
        return (self.floor, self.direction)

    def apply(self, sink: RequestSink) -> str:
        return sink.assign_hall_call(self)

    def label(self) -> str:
        suffix = "" if self.destination is None else f" to {self.destination}"
        return f"hall {self.floor}{self.direction.arrow()}{suffix}"


@dataclass(frozen=True, slots=True)
class CabinRequest:
    """A button inside a car: no dispatch decision to make, the car is already chosen."""

    car_id: str
    floor: int
    requested_at: float

    def apply(self, sink: RequestSink) -> str:
        return sink.assign_cabin_call(self)

    def label(self) -> str:
        return f"cabin {self.car_id} to {self.floor}"


# --8<-- [end:requests]


# --8<-- [start:entities]
@dataclass(slots=True)
class Door:
    """A dwell timer, not a motor: ``open`` starts it, ``tick`` runs it down."""

    default_hold: int = 2
    state: DoorState = DoorState.CLOSED
    hold_ticks: int = 0

    def is_open(self) -> bool:
        return self.state is not DoorState.CLOSED

    def open(self) -> None:
        self.state = DoorState.OPEN
        self.hold_ticks = self.default_hold

    def obstruct(self) -> None:
        if self.state is DoorState.CLOSED:
            raise DoorObstructedError("a closed door cannot be obstructed")
        self.state = DoorState.OBSTRUCTED
        self.hold_ticks = self.default_hold

    def clear_obstruction(self) -> None:
        if self.state is not DoorState.OBSTRUCTED:
            raise DoorObstructedError("the door is not obstructed")
        self.state = DoorState.OPEN
        self.hold_ticks = self.default_hold

    def tick(self) -> bool:
        """Run the dwell timer for one tick. Returns True on the tick it closes."""
        if self.state is DoorState.CLOSED:
            return False
        if self.state is DoorState.OBSTRUCTED:
            self.hold_ticks = self.default_hold  # a blocked door restarts its timer
            return False
        self.hold_ticks -= 1
        if self.hold_ticks > 0:
            return False
        self.state = DoorState.CLOSED
        return True


@dataclass(slots=True)
class Floor:
    """A landing with its two hall lamps. Pressing a lit lamp is absorbed here."""

    number: int
    lit: set[Direction] = field(default_factory=set)

    def press(self, direction: Direction) -> bool:
        """Light the lamp. Returns False when it was already lit (a duplicate press)."""
        if direction in self.lit:
            return False
        self.lit.add(direction)
        return True

    def clear(self, direction: Direction) -> None:
        self.lit.discard(direction)


@dataclass(frozen=True, slots=True)
class CarStatus:
    """An immutable snapshot: what a strategy scores and what a display shows."""

    car_id: str
    floor: int
    direction: Direction
    state: ElevatorState
    door: DoorState
    stops: tuple[int, ...]
    load: int
    capacity: int

    def in_service(self) -> bool:
        return self.state is not ElevatorState.MAINTENANCE

    def is_idle(self) -> bool:
        return self.state is ElevatorState.IDLE and not self.stops

    def has_room(self) -> bool:
        return self.load < self.capacity

    def distance_to(self, floor: int) -> int:
        return abs(self.floor - floor)

    def render(self) -> str:
        door = " door" if self.door is not DoorState.CLOSED else ""
        return f"{self.car_id}{self.direction.arrow()}{self.floor}{door}"


@dataclass(frozen=True, slots=True)
class ServedCall:
    """One answered hall call: the raw material of the average-wait comparison."""

    floor: int
    direction: Direction
    car_id: str
    requested_at: float
    served_at: float

    def wait(self) -> float:
        return self.served_at - self.requested_at


# --8<-- [end:entities]
