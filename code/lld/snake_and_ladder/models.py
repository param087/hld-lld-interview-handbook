"""The track, the jumps on it, and the rules that decide where a roll lands you.

Every rule that a variant board changes lives in ``GameConfig``; every rule that a
*broken* board would violate is enforced in ``Board.__init__``, once, at build time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from common import InvalidStateError, ValidationError

DEFAULT_SIZE = 100
MIN_SIZE = 4


# --8<-- [start:enums]
class JumpKind(StrEnum):
    SNAKE = "snake"  # end < start
    LADDER = "ladder"  # end > start


class OvershootRule(StrEnum):
    """What happens when a roll would take you past the last square."""

    STAY = "stay"  # you must land exactly; an overshoot forfeits the move
    BOUNCE = "bounce"  # you walk to the last square and back down by the excess
    ANY = "any"  # reaching or passing the last square wins


class InvalidJumpError(ValidationError):
    """The board is illegal: a jump is off the track, doubled, chained or cyclic."""


class GameFinishedError(InvalidStateError):
    """The game already has a winner."""


# --8<-- [end:enums]


# --8<-- [start:values]
@dataclass(frozen=True, slots=True, order=True)
class Position:
    """A square on the track. 0 means 'not on the board yet'; ``size`` is home."""

    square: int = 0

    def is_home(self, size: int) -> bool:
        return self.square == size


@dataclass(frozen=True, slots=True)
class Jump:
    """A snake or a ladder. Which one it is follows from the numbers, not from a flag."""

    start: int
    end: int

    @property
    def kind(self) -> JumpKind:
        return JumpKind.LADDER if self.end > self.start else JumpKind.SNAKE

    def __str__(self) -> str:
        return f"{self.kind.value} {self.start}-{self.end}"


@dataclass(frozen=True, slots=True)
class GameConfig:
    """Every rule a variant changes, in one immutable object."""

    size: int = DEFAULT_SIZE
    overshoot: OvershootRule = OvershootRule.STAY
    allow_chained_jumps: bool = False
    play_to_last: bool = False  # keep playing until only one player is left, for a full ranking

    def __post_init__(self) -> None:
        if self.size < MIN_SIZE:
            raise ValidationError(f"a board needs at least {MIN_SIZE} squares, got {self.size}")


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """One line of the game log: everything needed to audit or replay a turn."""

    number: int
    player: str
    roll: int
    start: Position
    end: Position
    jumps: tuple[Jump, ...]

    @property
    def blocked(self) -> bool:
        """True when the exact-finish rule forfeited the move."""
        return self.end == self.start and not self.jumps


# --8<-- [end:values]


# --8<-- [start:board]
class Board:
    """The track plus its jumps. Validating the board is most of this class.

    Three invariants are checked once, at construction, so no turn ever has to:
    a jump stays on the track, no two jumps start on the same square, and jumps do
    not chain (or, when chains are allowed, do not form a cycle). A cyclic board is
    the failure interviewers ask about: a token would jump forever.
    """

    def __init__(self, jumps: Iterable[Jump], config: GameConfig | None = None) -> None:
        self.config = config or GameConfig()
        self.size = self.config.size
        self._jumps: dict[int, Jump] = {}
        for jump in jumps:
            self._validate(jump)
            self._jumps[jump.start] = jump
        if self.config.allow_chained_jumps:
            self._reject_cycles()
        else:
            self._reject_chains()

    @property
    def jumps(self) -> dict[int, Jump]:
        return dict(self._jumps)

    def jump_at(self, square: int) -> Jump | None:
        return self._jumps.get(square)

    def landing_square(self, position: Position, roll: int) -> Position:
        """Apply the overshoot rule. Jumps are resolved separately, by ``step``."""
        target = position.square + roll
        if target <= self.size:
            return Position(target)
        if self.config.overshoot is OvershootRule.ANY:
            return Position(self.size)
        if self.config.overshoot is OvershootRule.BOUNCE:
            return Position(max(1, self.size - (target - self.size)))
        return position  # STAY: the move is forfeited

    def resolve(self, position: Position) -> tuple[Position, tuple[Jump, ...]]:
        """Follow the jump at this square, and the chain after it when chains are allowed."""
        taken: list[Jump] = []
        seen: set[int] = set()
        current = position
        while (jump := self._jumps.get(current.square)) is not None:
            if current.square in seen:  # unreachable: validation rejected cyclic boards
                raise InvalidStateError(f"jump cycle at square {current.square}")
            seen.add(current.square)
            taken.append(jump)
            current = Position(jump.end)
            if not self.config.allow_chained_jumps:
                break
        return current, tuple(taken)

    def step(self, position: Position, roll: int) -> tuple[Position, tuple[Jump, ...]]:
        """One roll: overshoot rule first, then jumps."""
        landed = self.landing_square(position, roll)
        if landed == position:
            return position, ()
        return self.resolve(landed)

    def _validate(self, jump: Jump) -> None:
        if not (1 <= jump.start <= self.size and 1 <= jump.end <= self.size):
            raise InvalidJumpError(f"{jump} leaves a board of {self.size} squares")
        if jump.start == jump.end:
            raise InvalidJumpError(f"{jump} goes nowhere")
        if jump.start == self.size:
            raise InvalidJumpError(f"{jump} starts on the last square")
        if jump.start in self._jumps:
            raise InvalidJumpError(f"{jump} overlaps {self._jumps[jump.start]} on square {jump.start}")

    def _reject_chains(self) -> None:
        for jump in self._jumps.values():
            if jump.end in self._jumps:
                raise InvalidJumpError(f"{jump} lands on the head of {self._jumps[jump.end]}")

    def _reject_cycles(self) -> None:
        for start, jump in self._jumps.items():
            seen = {start}
            square = jump.end
            while square in self._jumps:
                if square in seen:
                    raise InvalidJumpError(f"the chain from square {start} loops back to {square}")
                seen.add(square)
                square = self._jumps[square].end


# --8<-- [end:board]
