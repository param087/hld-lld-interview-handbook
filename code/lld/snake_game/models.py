"""The grid, the snake and the vocabulary of a tick.

The two rules that make or break this problem live here: ``Snake.would_collide``
(the tail's cell is free the moment the tail leaves it) and ``Direction.opposite``
(no 180-degree reversal). Everything else is bookkeeping.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from common import InvalidStateError, ValidationError


# --8<-- [start:enums]
class GameState(StrEnum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    OVER = "over"


class EndReason(StrEnum):
    WALL = "wall"
    SELF = "self"
    OBSTACLE = "obstacle"
    FILLED = "filled"  # no free cell left: the snake filled the grid and won


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"

    @property
    def delta(self) -> tuple[int, int]:
        return DELTAS[self]

    @property
    def opposite(self) -> Direction:
        return OPPOSITES[self]

    def reverses(self, other: Direction) -> bool:
        return self is other.opposite


class GameOverError(InvalidStateError):
    """The game has already ended; there is nothing left to tick."""


# --8<-- [end:enums]


# --8<-- [start:geometry]
@dataclass(frozen=True, slots=True, order=True)
class Point:
    """A grid cell. ``y`` grows downwards, so ``UP`` is ``-1`` on ``y``."""

    x: int
    y: int

    def step(self, direction: Direction) -> Point:
        dx, dy = direction.delta
        return Point(self.x + dx, self.y + dy)

    def __str__(self) -> str:
        return f"({self.x},{self.y})"


DELTAS: Final[Mapping[Direction, tuple[int, int]]] = MappingProxyType(
    {
        Direction.UP: (0, -1),
        Direction.DOWN: (0, 1),
        Direction.LEFT: (-1, 0),
        Direction.RIGHT: (1, 0),
    }
)
OPPOSITES: Final[Mapping[Direction, Direction]] = MappingProxyType(
    {
        Direction.UP: Direction.DOWN,
        Direction.DOWN: Direction.UP,
        Direction.LEFT: Direction.RIGHT,
        Direction.RIGHT: Direction.LEFT,
    }
)


@dataclass(frozen=True, slots=True)
class Food:
    position: Point
    value: int = 1


@dataclass(frozen=True, slots=True)
class TickResult:
    """What one tick did. Immutable, so it can be pushed to observers and stored."""

    tick: int
    state: GameState
    moved: bool
    ate: bool
    score: int
    reason: EndReason | None = None


@dataclass(frozen=True, slots=True)
class Frame:
    """One rendered picture plus the numbers a UI puts around it."""

    tick: int
    state: GameState
    score: int
    board: str


# --8<-- [end:geometry]


# --8<-- [start:grid]
@dataclass(slots=True)
class Grid:
    """Bounds and obstacles. It knows geometry, never the snake."""

    width: int
    height: int
    obstacles: frozenset[Point] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.width < 3 or self.height < 3:
            raise ValidationError(f"grid must be at least 3x3, got {self.width}x{self.height}")
        outside = [p for p in self.obstacles if not self.contains(p)]
        if outside:
            raise ValidationError(f"obstacles outside the grid: {sorted(outside)}")

    def contains(self, point: Point) -> bool:
        return 0 <= point.x < self.width and 0 <= point.y < self.height

    def is_obstacle(self, point: Point) -> bool:
        return point in self.obstacles

    def free_cells(self, occupied: Iterable[Point]) -> list[Point]:
        """Row-major order, so a seeded spawner is reproducible across runs."""
        taken = set(occupied) | self.obstacles
        return [
            Point(x, y) for y in range(self.height) for x in range(self.width) if Point(x, y) not in taken
        ]


# --8<-- [end:grid]


# --8<-- [start:snake]
class Snake:
    """A deque for order and a set for membership: O(1) push, pop and self-collision.

    The set is what makes the collision test O(1) instead of O(length). Keeping the
    two containers in step is the whole contract of this class, so every mutation
    goes through ``move``.
    """

    def __init__(self, body: Sequence[Point]) -> None:
        if not body:
            raise ValidationError("a snake needs at least one cell")
        if len(set(body)) != len(body):
            raise ValidationError("a snake cannot start on top of itself")
        self._body: deque[Point] = deque(body)
        self._cells: set[Point] = set(body)

    @property
    def head(self) -> Point:
        return self._body[0]

    @property
    def tail(self) -> Point:
        return self._body[-1]

    def __len__(self) -> int:
        return len(self._body)

    def occupies(self, point: Point) -> bool:
        return point in self._cells

    def cells(self) -> tuple[Point, ...]:
        return tuple(self._body)

    def next_head(self, direction: Direction) -> Point:
        return self.head.step(direction)

    def would_collide(self, target: Point, grow: bool) -> bool:
        """Self-collision, with the rule everyone gets wrong.

        The tail's cell is *free* this tick, because the tail leaves it in the same
        move - unless the snake is growing, in which case the tail stays put.
        """
        if target not in self._cells:
            return False
        return grow or target != self.tail

    def move(self, target: Point, grow: bool) -> Point | None:
        """Push the new head, drop the tail unless growing. Returns the vacated cell."""
        if self.would_collide(target, grow):
            raise InvalidStateError(f"{target} is occupied by the snake")
        vacated: Point | None = None
        if not grow:
            vacated = self._body.pop()
            self._cells.discard(vacated)
        self._body.appendleft(target)
        self._cells.add(target)
        return vacated


# --8<-- [end:snake]
