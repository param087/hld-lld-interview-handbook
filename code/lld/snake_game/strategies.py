"""Where food appears and how fast the clock runs: the two policies a difficulty setting changes."""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable
from typing import Protocol

from common import ValidationError
from lld.snake_game.models import Food, Grid, Point


# --8<-- [start:spawner]
class FoodSpawner(Protocol):
    """Returns the next piece of food, or None when the grid has no free cell left."""

    def spawn(self, grid: Grid, occupied: Iterable[Point]) -> Food | None: ...


class RandomFoodSpawner:
    """Uniform over the free cells. The generator is injected, so a game replays exactly."""

    def __init__(self, rng: random.Random, value: int = 1) -> None:
        if value < 1:
            raise ValidationError("food must be worth at least one point")
        self._rng = rng
        self._value = value

    def spawn(self, grid: Grid, occupied: Iterable[Point]) -> Food | None:
        free = grid.free_cells(occupied)
        if not free:
            return None
        return Food(self._rng.choice(free), self._value)


class ScriptedFoodSpawner:
    """A queue of positions. This is what turns a snake game into a unit test."""

    def __init__(self, positions: Iterable[Point], value: int = 1) -> None:
        self._queue: deque[Point] = deque(positions)
        self._value = value

    def spawn(self, grid: Grid, occupied: Iterable[Point]) -> Food | None:
        taken = set(occupied)
        while self._queue:
            position = self._queue.popleft()
            if grid.contains(position) and position not in taken and not grid.is_obstacle(position):
                return Food(position, self._value)
        return None


# --8<-- [end:spawner]


# --8<-- [start:speed]
class SpeedPolicy(Protocol):
    """Seconds between ticks. The engine asks; it never computes a difficulty itself."""

    def interval(self, score: int, length: int) -> float: ...


class ConstantSpeed:
    def __init__(self, seconds: float = 0.15) -> None:
        if seconds <= 0:
            raise ValidationError("a tick interval must be positive")
        self._seconds = seconds

    def interval(self, score: int, length: int) -> float:
        return self._seconds


class AcceleratingSpeed:
    """Every point shortens the interval by ``factor``, down to a floor.

    With the defaults a game starts at 200 ms per tick and reaches the 60 ms floor
    after 12 points: 0.2 x 0.9^12 = 0.056, clamped to 0.06.
    """

    def __init__(self, base: float = 0.2, factor: float = 0.9, floor: float = 0.06) -> None:
        if not (0 < factor <= 1) or base <= 0 or floor <= 0 or floor > base:
            raise ValidationError("need 0 < factor <= 1 and 0 < floor <= base")
        self._base = base
        self._factor = factor
        self._floor = floor

    def interval(self, score: int, length: int) -> float:
        return max(self._floor, self._base * self._factor**score)


# --8<-- [end:speed]
