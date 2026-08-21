"""Dice as a Strategy: the one thing a test must control and a variant must replace."""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable, Sequence
from typing import Protocol

from common import InvalidStateError, ValidationError


# --8<-- [start:dice]
class DiceStrategy(Protocol):
    """Roll something. The game never asks how many dice there are or how fair they are."""

    def roll(self) -> int: ...

    @property
    def max_roll(self) -> int: ...


class FairDice:
    """``count`` uniform dice of ``sides`` faces each. The generator is injected, always."""

    def __init__(self, rng: random.Random, sides: int = 6, count: int = 1) -> None:
        if sides < 2 or count < 1:
            raise ValidationError("dice need at least 2 sides and at least 1 die")
        self._rng = rng
        self.sides = sides
        self.count = count

    def roll(self) -> int:
        return sum(self._rng.randint(1, self.sides) for _ in range(self.count))

    @property
    def max_roll(self) -> int:
        return self.sides * self.count


class LoadedDice:
    """A weighted die: the classic "is this board still winnable" follow-up in one class."""

    def __init__(self, rng: random.Random, weights: Sequence[int]) -> None:
        if len(weights) < 2 or any(w < 0 for w in weights) or sum(weights) == 0:
            raise ValidationError("weights must cover at least 2 faces and not all be zero")
        self._rng = rng
        self._faces = list(range(1, len(weights) + 1))
        self._weights = list(weights)

    def roll(self) -> int:
        return self._rng.choices(self._faces, weights=self._weights, k=1)[0]

    @property
    def max_roll(self) -> int:
        return self._faces[-1]


class ScriptedDice:
    """A queue of rolls. This is how a game becomes a unit test instead of a simulation."""

    def __init__(self, rolls: Iterable[int]) -> None:
        self._queue: deque[int] = deque(rolls)
        self._max = max(self._queue, default=1)

    def roll(self) -> int:
        if not self._queue:
            raise InvalidStateError("the dice script ran out")
        return self._queue.popleft()

    @property
    def max_roll(self) -> int:
        return self._max


# --8<-- [end:dice]
