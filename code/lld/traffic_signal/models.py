"""Approaches, signal states, phases and the button presses that arrive as commands.

The safety invariant lives here: a `Phase` whose movements conflict cannot be
constructed, so no amount of scheduling logic downstream can turn two crossing
approaches green at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from common import InvalidStateError, NotFoundError, ValidationError


# --8<-- [start:enums]
class Direction(StrEnum):
    """One approach to the intersection, named by where the traffic comes from."""

    NORTH = "north"
    SOUTH = "south"
    EAST = "east"
    WEST = "west"

    def opposite(self) -> Direction:
        pairs = {
            Direction.NORTH: Direction.SOUTH,
            Direction.SOUTH: Direction.NORTH,
            Direction.EAST: Direction.WEST,
            Direction.WEST: Direction.EAST,
        }
        return pairs[self]

    def conflicts_with(self, other: Direction) -> bool:
        """Opposing through movements are compatible; anything crossing is not."""
        return other is not self and other is not self.opposite()


class SignalState(StrEnum):
    """What one signal head is showing."""

    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    FLASHING_RED = "flashing_red"


class ControllerState(StrEnum):
    """Where the intersection is in its cycle. Only GREEN and EMERGENCY show green."""

    GREEN = "green"
    YELLOW = "yellow"
    ALL_RED = "all_red"
    EMERGENCY = "emergency"
    MAINTENANCE = "maintenance"


class PedestrianState(StrEnum):
    WALK = "walk"
    DONT_WALK = "dont_walk"


# --8<-- [end:enums]


# --8<-- [start:errors]
class ConflictingMovementError(ValidationError):
    """Two movements in the same phase would cross each other."""


class UnknownApproachError(NotFoundError):
    """No phase in this cycle ever gives that approach a green."""


class SignalStateError(InvalidStateError):
    """The operation is not legal for the controller's current state."""


# --8<-- [end:errors]


# --8<-- [start:phases]
@dataclass(frozen=True, slots=True)
class Phase:
    """A set of movements that may run together, and the bounds on its green.

    The constructor is the safety check: `Phase("bad", {NORTH, EAST})` raises,
    so an unsafe phase never exists to be scheduled.
    """

    name: str
    movements: frozenset[Direction]
    min_green: int = 6
    max_green: int = 20

    def __post_init__(self) -> None:
        if not self.movements:
            raise ValidationError(f"phase {self.name!r} has no movements")
        for one in self.movements:
            for other in self.movements:
                if one.conflicts_with(other):
                    raise ConflictingMovementError(
                        f"phase {self.name!r}: {one} and {other} cannot be green together"
                    )
        if not 1 <= self.min_green <= self.max_green:
            raise ValidationError(f"phase {self.name!r} has an impossible green range")

    def serves(self, direction: Direction) -> bool:
        return direction in self.movements

    def clamp(self, ticks: int) -> int:
        return max(self.min_green, min(self.max_green, ticks))


@dataclass(frozen=True, slots=True)
class PhaseCycle:
    """The ring: the fixed order phases are offered in."""

    phases: tuple[Phase, ...]

    def __post_init__(self) -> None:
        if not self.phases:
            raise ValidationError("a cycle needs at least one phase")
        names = [phase.name for phase in self.phases]
        if len(set(names)) != len(names):
            raise ValidationError("phase names must be unique within a cycle")

    def __len__(self) -> int:
        return len(self.phases)

    def phase(self, index: int) -> Phase:
        return self.phases[index % len(self.phases)]

    def index_of(self, name: str) -> int:
        for index, phase in enumerate(self.phases):
            if phase.name == name:
                return index
        raise UnknownApproachError(f"no phase named {name!r}")

    def serving(self, direction: Direction) -> Phase:
        for phase in self.phases:
            if phase.serves(direction):
                return phase
        raise UnknownApproachError(f"no phase serves {direction}")

    def directions(self) -> tuple[Direction, ...]:
        seen: list[Direction] = []
        for phase in self.phases:
            seen.extend(d for d in sorted(phase.movements) if d not in seen)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class PhaseDemand:
    """What the timing strategy is allowed to know: the phase and who is waiting."""

    phase: Phase
    waiting_vehicles: int
    pedestrian_waiting: bool


# --8<-- [end:phases]


# --8<-- [start:commands]
class CommandSink(Protocol):
    """The three controller methods a queued command needs."""

    def apply_pedestrian_call(self, direction: Direction) -> str: ...

    def apply_emergency(self, direction: Direction) -> str: ...

    def apply_clear_override(self) -> str: ...


class SignalCommand(Protocol):
    """Command: a button press as an object, executed at the next safe point."""

    requested_at: float

    def apply(self, sink: CommandSink) -> str: ...

    def label(self) -> str: ...


@dataclass(frozen=True, slots=True)
class PedestrianCall:
    """Someone wants to cross alongside `direction`; that phase must not be skipped."""

    direction: Direction
    requested_at: float

    def apply(self, sink: CommandSink) -> str:
        return sink.apply_pedestrian_call(self.direction)

    def label(self) -> str:
        return f"pedestrian call on {self.direction}"


@dataclass(frozen=True, slots=True)
class EmergencyOverride:
    """Hold a green for `direction` until it is cleared."""

    direction: Direction
    requested_at: float

    def apply(self, sink: CommandSink) -> str:
        return sink.apply_emergency(self.direction)

    def label(self) -> str:
        return f"emergency override for {self.direction}"


@dataclass(frozen=True, slots=True)
class ClearOverride:
    """End the override and let the ring resume."""

    requested_at: float

    def apply(self, sink: CommandSink) -> str:
        return sink.apply_clear_override()

    def label(self) -> str:
        return "clear override"


# --8<-- [end:commands]


# --8<-- [start:observer]
@dataclass(frozen=True, slots=True)
class SignalUpdate:
    """What the controller publishes after every tick. Heads decide their own colour."""

    tick: int
    phase: str
    movements: frozenset[Direction]
    stage: ControllerState


@dataclass(frozen=True, slots=True)
class SignalEvent:
    tick: int
    at: float
    message: str

    def render(self) -> str:
        return f"t{self.tick} {self.message}"


class PhaseListener(Protocol):
    """Observer: signal heads, pedestrian signals, telemetry."""

    def on_phase_changed(self, update: SignalUpdate) -> None: ...


class Sensor(Protocol):
    """An induction loop. It reports; it never acts."""

    def waiting(self, direction: Direction) -> int: ...


# --8<-- [end:observer]
