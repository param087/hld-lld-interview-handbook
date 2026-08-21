"""Template Method: the skeleton of an algorithm in a base class, with steps filled in by subclasses.

The running example is a turn-based board game. ``BoardGame.play`` fixes the order
of every game played here (set up, alternate turns until the game ends, report)
and leaves the steps that differ, how a move is chosen, how it is applied and when
the game is over, to ``TicTacToe`` and ``SnakeAndLadder``. The last section
restates the skeleton as a function that takes the steps as callables, the
Pythonic form when the variable parts are small.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Final

from common import InvalidStateError, ValidationError

DEFAULT_TURN_LIMIT = 1_000
# The classic board's snakes and ladders, read-only so the module holds no mutable state.
JUMPS: Final[Mapping[int, int]] = MappingProxyType(
    {4: 14, 9: 31, 17: 7, 20: 38, 28: 84, 40: 59, 51: 67, 54: 34, 62: 19, 64: 60, 87: 24, 93: 73, 95: 75, 99: 78}
)


# --8<-- [start:template]
@dataclass(frozen=True, slots=True)
class GameResult:
    winner: str | None  # None is a draw
    turns: int


class BoardGame[M](ABC):
    """The template: ``play`` owns the order of the steps and never changes per game.

    Abstract steps (``choose_move``, ``apply_move``, ``is_over``, ``winner``) must be
    supplied by every game. Hooks (``setup``, ``after_move``) have a harmless default
    and are overridden only by games that want them. Turn rotation, the turn limit
    and the log are shared, which is the point: a concrete game is only its rules.
    ``M`` is the game's move type: a cell index, a dice roll, a chess move.
    """

    def __init__(self, players: Sequence[str], turn_limit: int = DEFAULT_TURN_LIMIT) -> None:
        if len(players) < 2:
            raise ValidationError("a board game needs at least two players")
        if len(set(players)) != len(players):
            raise ValidationError("player names must be unique")
        self.players = tuple(players)
        self.turn_limit = turn_limit
        self.log: list[str] = []

    def play(self) -> GameResult:
        """The template method: subclasses never override it."""
        self.setup()
        turns = 0
        while not self.is_over():
            if turns >= self.turn_limit:
                raise InvalidStateError(f"no result after {turns} turns")
            player = self.players[turns % len(self.players)]
            move = self.choose_move(player)
            self.apply_move(player, move)
            self.after_move(player, move)
            turns += 1
        return GameResult(self.winner(), turns)

    # -- hooks: a default every game can live with ---------------------------------------
    def setup(self) -> None:
        """Called once before the first turn."""
        self.log.clear()

    def after_move(self, player: str, move: M) -> None:
        """Called after every applied move."""
        self.log.append(f"{player} -> {move}")

    # -- abstract steps: every game supplies them ---------------------------------------
    @abstractmethod
    def choose_move(self, player: str) -> M: ...

    @abstractmethod
    def apply_move(self, player: str, move: M) -> None: ...

    @abstractmethod
    def is_over(self) -> bool: ...

    @abstractmethod
    def winner(self) -> str | None: ...


# --8<-- [end:template]


# --8<-- [start:games]
class TicTacToe(BoardGame[int]):
    """Only the rules: a 3x3 board, a scripted move source, three in a row wins.

    In the full problem ``choose_move`` delegates to a ``MoveStrategy`` (human input,
    random bot, minimax); a scripted iterator keeps the demo and the tests exact.
    """

    LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))

    def __init__(self, moves: Iterable[int], players: Sequence[str] = ("X", "O")) -> None:
        super().__init__(players)
        self._moves: Iterator[int] = iter(moves)
        self.board: list[str] = [" "] * 9

    def setup(self) -> None:
        super().setup()
        self.board = [" "] * 9

    def choose_move(self, player: str) -> int:
        try:
            return next(self._moves)
        except StopIteration:
            raise InvalidStateError(f"{player} has no scripted move left") from None

    def apply_move(self, player: str, move: int) -> None:
        if not 0 <= move < 9:
            raise ValidationError(f"cell {move} is off the board")
        if self.board[move] != " ":
            raise ValidationError(f"cell {move} is taken")
        self.board[move] = player

    def is_over(self) -> bool:
        return self.winner() is not None or " " not in self.board

    def winner(self) -> str | None:
        for a, b, c in self.LINES:
            if self.board[a] != " " and self.board[a] == self.board[b] == self.board[c]:
                return self.board[a]
        return None

    def rows(self) -> list[str]:
        return ["".join(self.board[i : i + 3]) for i in (0, 3, 6)]


class SnakeAndLadder(BoardGame[int]):
    """Only the rules: roll, move, take the snake or ladder, first to the last square wins."""

    def __init__(
        self,
        players: Sequence[str],
        roll: Callable[[], int],
        jumps: Mapping[int, int],
        size: int = 100,
    ) -> None:
        super().__init__(players)
        for start, end in jumps.items():
            if not (1 <= start < size and 1 <= end <= size) or start == end:
                raise ValidationError(f"jump {start} -> {end} is off the board")
            if end in jumps:
                raise ValidationError(f"jump {start} -> {end} chains into another jump")
        self._roll = roll
        self._jumps = dict(jumps)
        self.size = size
        self.positions: dict[str, int] = dict.fromkeys(self.players, 0)

    def setup(self) -> None:
        super().setup()
        self.positions = dict.fromkeys(self.players, 0)

    def choose_move(self, player: str) -> int:
        return self._roll()

    def apply_move(self, player: str, move: int) -> None:
        if move < 1:
            raise ValidationError(f"a roll of {move} is not a move")
        target = self.positions[player] + move
        if target <= self.size:  # an overshoot stays put: the exact-finish rule
            self.positions[player] = self._jumps.get(target, target)

    def after_move(self, player: str, move: int) -> None:
        self.log.append(f"{player} rolled {move} -> {self.positions[player]}")

    def is_over(self) -> bool:
        return self.winner() is not None

    def winner(self) -> str | None:
        return next((p for p, square in self.positions.items() if square == self.size), None)


# --8<-- [end:games]


# --8<-- [start:functional]
def play_turns(
    players: Sequence[str],
    take_turn: Callable[[str], None],
    is_over: Callable[[], bool],
    winner: Callable[[], str | None],
    turn_limit: int = DEFAULT_TURN_LIMIT,
) -> GameResult:
    """The same skeleton as a function: the steps arrive as arguments instead of overrides."""
    turns = 0
    while not is_over():
        if turns >= turn_limit:
            raise InvalidStateError(f"no result after {turns} turns")
        take_turn(players[turns % len(players)])
        turns += 1
    return GameResult(winner(), turns)


def snake_and_ladder_with_closures(
    players: Sequence[str], roll: Callable[[], int], jumps: Mapping[int, int], size: int = 100
) -> GameResult:
    """Snake and ladder in a dozen lines: the state is closed over instead of stored on ``self``."""
    positions = dict.fromkeys(players, 0)

    def take_turn(player: str) -> None:
        target = positions[player] + roll()
        if target <= size:
            positions[player] = jumps.get(target, target)

    def winner() -> str | None:
        return next((p for p, square in positions.items() if square == size), None)

    return play_turns(players, take_turn, is_over=lambda: winner() is not None, winner=winner)


# --8<-- [end:functional]


def main() -> None:
    print("--- tic-tac-toe: the skeleton runs a scripted game ---")
    game = TicTacToe(moves=[4, 0, 2, 6, 5, 1, 8])
    result = game.play()
    for row in game.rows():
        print(f"  {row}")
    print(f"log: {', '.join(game.log)}")
    print(f"result: {result.winner} wins after {result.turns} turns")

    draw = TicTacToe(moves=[0, 1, 2, 4, 3, 5, 7, 6, 8]).play()
    print(f"a full board with no line: winner={draw.winner} after {draw.turns} turns")

    print("--- snake and ladder: the same skeleton, different steps, seeded dice ---")
    ladder_game = SnakeAndLadder(["Ann", "Bob"], roll=partial(random.Random(42).randint, 1, 6), jumps=JUMPS)
    result = ladder_game.play()
    for line in ladder_game.log[:3]:
        print(f"  {line}")
    print(f"  ... {len(ladder_game.log) - 3} more turns")
    print(f"result: {result.winner} wins after {result.turns} turns")

    print("--- closures: the skeleton as a function, same seed, same game ---")
    closure_result = snake_and_ladder_with_closures(
        ["Ann", "Bob"], roll=partial(random.Random(42).randint, 1, 6), jumps=JUMPS
    )
    print(f"result: {closure_result.winner} wins after {closure_result.turns} turns")

    print("--- a rule violation surfaces from a step; the skeleton stays untouched ---")
    try:
        TicTacToe(moves=[4, 4]).play()
    except ValidationError as exc:
        print(f"rejected: {exc}")


if __name__ == "__main__":
    main()
