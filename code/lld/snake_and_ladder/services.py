"""The game: a thin subclass of the shared ``BoardGame`` template, plus a board factory."""

from __future__ import annotations

import random
from collections.abc import Sequence

from common import ValidationError
from lld.snake_and_ladder.models import (
    Board,
    GameConfig,
    Jump,
    Position,
    TurnRecord,
)
from lld.snake_and_ladder.strategies import DiceStrategy
from lld.tic_tac_toe.base import DEFAULT_TURN_LIMIT, BoardGame

# The Milton Bradley layout: nine ladders, ten snakes, no jump landing on another jump's head.
CLASSIC_JUMPS: tuple[Jump, ...] = (
    Jump(1, 38),
    Jump(4, 14),
    Jump(9, 31),
    Jump(21, 42),
    Jump(28, 84),
    Jump(36, 44),
    Jump(51, 67),
    Jump(71, 91),
    Jump(80, 100),
    Jump(16, 6),
    Jump(47, 26),
    Jump(49, 11),
    Jump(56, 53),
    Jump(62, 19),
    Jump(64, 60),
    Jump(87, 24),
    Jump(93, 73),
    Jump(95, 75),
    Jump(98, 78),
)


# --8<-- [start:factory]
class BoardFactory:
    """Factory: callers ask for a *kind* of board and get a validated one, or an error."""

    @staticmethod
    def classic(config: GameConfig | None = None) -> Board:
        return Board(CLASSIC_JUMPS, config)

    @staticmethod
    def random_board(
        rng: random.Random,
        snakes: int = 8,
        ladders: int = 8,
        config: GameConfig | None = None,
    ) -> Board:
        """Sample 2 x (snakes + ladders) distinct squares and pair them up.

        Because every square is used at most once, no two jumps can share a head and
        no jump can land on another jump's head - the board is valid by construction
        instead of by retrying until ``Board`` stops complaining.
        """
        config = config or GameConfig()
        needed = 2 * (snakes + ladders)
        if needed > config.size - 2:
            raise ValidationError(f"{needed} squares needed but only {config.size - 2} are usable")
        squares = rng.sample(range(2, config.size), needed)
        pairs = [(squares[2 * i], squares[2 * i + 1]) for i in range(snakes + ladders)]
        jumps = [Jump(min(a, b), max(a, b)) for a, b in pairs[:ladders]]
        jumps += [Jump(max(a, b), min(a, b)) for a, b in pairs[ladders:]]
        return Board(jumps, config)


# --8<-- [end:factory]


# --8<-- [start:game]
class SnakeAndLadderGame(BoardGame[int]):
    """Snake and ladder is five overrides on the shared skeleton.

    Compare it with ``TicTacToeGame``: same ``play`` loop, same turn cursor, same
    observers, same lock. What differs is what a move *is* (a dice roll rather than a
    cell), and one hook - ``advance_turn`` skips players who already went home when
    the table is playing for full rankings.
    """

    MIN_PLAYERS = 2
    MAX_PLAYERS = 8

    def __init__(
        self,
        players: Sequence[str],
        dice: DiceStrategy,
        board: Board | None = None,
        *,
        turn_limit: int = DEFAULT_TURN_LIMIT,
    ) -> None:
        super().__init__(players, turn_limit=turn_limit)
        self.board = board or BoardFactory.classic()
        self.dice = dice
        self._positions: dict[str, Position] = dict.fromkeys(self.players, Position(0))
        self._finished: list[str] = []
        self._records: list[TurnRecord] = []

    # -- the template's steps ---------------------------------------------------------
    def setup(self) -> None:
        self._positions = dict.fromkeys(self.players, Position(0))
        self._finished.clear()
        self._records.clear()

    def choose_move(self, player: str) -> int:
        return self.dice.roll()

    def apply_move(self, player: str, move: int) -> None:
        if move < 1:
            raise ValidationError(f"a roll of {move} is not a move")
        start = self._positions[player]
        end, jumps = self.board.step(start, move)
        self._positions[player] = end
        self._records.append(TurnRecord(len(self._records) + 1, player, move, start, end, jumps))
        if end.is_home(self.board.size) and player not in self._finished:
            self._finished.append(player)

    def after_move(self, player: str, move: int) -> None:
        self.emit(self._describe(self._records[-1]), actor=player)

    def is_over(self) -> bool:
        if self.board.config.play_to_last:
            return len(self._finished) >= len(self.players) - 1
        return bool(self._finished)

    def winner(self) -> str | None:
        return self._finished[0] if self._finished else None

    def advance_turn(self) -> None:
        """Hook override: a player who is already home no longer takes a turn."""
        for _ in range(len(self.players)):
            super().advance_turn()
            if self.players[self._turn_index] not in self._finished:
                return

    # -- what the template does not give you -------------------------------------------
    def take_turn(self, player: str) -> TurnRecord:
        """The externally driven turn: whoever calls out of order is told so."""
        with self._lock:
            self.require_turn(player)
            self.play_turn()
            return self._records[-1]

    def position(self, player: str) -> Position:
        with self._lock:
            return self._positions[player]

    def records(self) -> tuple[TurnRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def ranking(self) -> list[str]:
        """Finishers in the order they went home, then the rest by how far they got."""
        with self._lock:
            unfinished = sorted(
                (p for p in self.players if p not in self._finished),
                key=lambda p: (-self._positions[p].square, self.players.index(p)),
            )
            return [*self._finished, *unfinished]

    def _describe(self, record: TurnRecord) -> str:
        if record.blocked:
            needed = self.board.size - record.start.square
            return f"{record.player} rolls {record.roll}, needs exactly {needed}, stays on {record.start.square}"
        trail = "".join(f" via {jump}" for jump in record.jumps)
        return f"{record.player} rolls {record.roll}: {record.start.square} to {record.end.square}{trail}"


# --8<-- [end:game]
