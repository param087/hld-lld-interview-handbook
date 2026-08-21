"""The game itself: turn enforcement, undo, replay, the player factory and the renderer."""

from __future__ import annotations

import random
import threading
from collections.abc import Callable, Sequence

from common import ValidationError
from lld.tic_tac_toe.base import (
    DEFAULT_TURN_LIMIT,
    BoardGame,
    GameEvent,
    GameStatus,
    TurnCursor,
)
from lld.tic_tac_toe.models import (
    DEFAULT_SIZE,
    Board,
    Cell,
    Move,
    NothingToUndoError,
    Player,
    PlayerKind,
    Symbol,
)
from lld.tic_tac_toe.strategies import (
    MinimaxMove,
    MoveStrategy,
    RandomMove,
    ScriptedMove,
    require_rng,
)

StrategyBuilder = Callable[[Sequence[Cell] | None, random.Random | None], MoveStrategy]


# --8<-- [start:factory]
def _human(moves: Sequence[Cell] | None, rng: random.Random | None) -> MoveStrategy:
    return ScriptedMove(moves or ())


def _random_bot(moves: Sequence[Cell] | None, rng: random.Random | None) -> MoveStrategy:
    return RandomMove(require_rng(rng))


def _perfect_bot(moves: Sequence[Cell] | None, rng: random.Random | None) -> MoveStrategy:
    return MinimaxMove()


class PlayerFactory:
    """Factory Method: callers name a *kind*, never a strategy class.

    Adding "cautious bot" is a builder plus a registry entry; the game, the board
    and the renderer are untouched.
    """

    _BUILDERS: dict[PlayerKind, StrategyBuilder] = {
        PlayerKind.HUMAN: _human,
        PlayerKind.RANDOM_BOT: _random_bot,
        PlayerKind.PERFECT_BOT: _perfect_bot,
    }

    @classmethod
    def create(
        cls,
        kind: PlayerKind | str,
        player_id: str,
        symbol: Symbol,
        *,
        moves: Sequence[Cell] | None = None,
        rng: random.Random | None = None,
    ) -> Player:
        try:
            resolved = PlayerKind(kind)
        except ValueError as exc:
            raise ValidationError(f"unknown player kind: {kind!r}") from exc
        return Player(player_id, symbol, resolved, cls._BUILDERS[resolved](moves, rng))


# --8<-- [end:factory]


# --8<-- [start:game]
class TicTacToeGame(BoardGame[Cell]):
    """Tic-tac-toe as a set of rules bolted onto the shared ``BoardGame`` skeleton.

    The five methods below are everything this game adds to the template: how a move
    is chosen, how it is applied, when the game is over, who won, and how to set up.
    Undo, replay and the input buffer are the features an interviewer asks for after
    the happy path works.
    """

    MIN_PLAYERS = 2
    MAX_PLAYERS = 2

    def __init__(
        self,
        players: Sequence[Player],
        size: int = DEFAULT_SIZE,
        *,
        turn_limit: int = DEFAULT_TURN_LIMIT,
    ) -> None:
        super().__init__([p.id for p in players], turn_limit=turn_limit)
        if len({p.symbol for p in players}) != len(players):
            raise ValidationError("the two players must use different symbols")
        self._by_id: dict[str, Player] = {p.id: p for p in players}
        self.board = Board(size)
        self._history: list[Move] = []
        self._cursors: list[TurnCursor] = []
        self._winner: str | None = None
        self._buffered: Cell | None = None

    # -- the template's steps ---------------------------------------------------------
    def setup(self) -> None:
        self.board.reset()
        self._history.clear()
        self._cursors.clear()
        self._winner = None

    def choose_move(self, player: str) -> Cell:
        """An externally submitted move wins over the player's own strategy."""
        if self._buffered is not None:
            cell, self._buffered = self._buffered, None
            return cell
        return self._by_id[player].next_move(self.board)

    def apply_move(self, player: str, move: Cell) -> None:
        symbol = self._by_id[player].symbol
        cursor = self.cursor()  # Memento taken *before* the board changes
        won = self.board.place(move, symbol)  # raises on an occupied or off-board cell
        self._cursors.append(cursor)
        self._history.append(Move(len(self._history) + 1, player, symbol, move))
        if won:
            self._winner = player

    def after_move(self, player: str, move: Cell) -> None:
        self.emit(f"{player} plays {self._by_id[player].symbol.value} at {move}", actor=player)

    def is_over(self) -> bool:
        return self._winner is not None or self.board.is_full()

    def winner(self) -> str | None:
        return self._winner

    # -- what the template does not give you -------------------------------------------
    def submit_move(self, player: str, cell: Cell) -> Move:
        """The UI path: one externally supplied move, turn-checked before anything moves."""
        with self._lock:
            self.require_turn(player)
            self._buffered = cell
            try:
                self.play_turn()
            finally:
                self._buffered = None
            return self._history[-1]

    def undo(self) -> Move:
        """Take back the last move: invert the board change, restore the cursor Memento."""
        try:
            with self._lock:
                if not self._history:
                    raise NothingToUndoError("no move to undo")
                move = self._history.pop()
                self.board.clear(move.cell)
                self._winner = None  # only the last move can have completed a line
                self.restore(self._cursors.pop())
                self.emit(f"undo: {move.player_id} takes back {move.cell}", actor=move.player_id)
                return move
        finally:
            self.flush_events()

    def history(self) -> tuple[Move, ...]:
        with self._lock:
            return tuple(self._history)

    def replay(self) -> list[str]:
        """Rebuild every position from the move log; frame 0 is the empty board."""
        board = Board(self.board.size)
        frames = [board.render()]
        for move in self.history():
            board.place(move.cell, move.symbol)
            frames.append(board.render())
        return frames


# --8<-- [end:game]


# --8<-- [start:renderer]
class BoardRenderer:
    """Observer: the game emits events, the renderer decides what a screen looks like.

    It stores only text, so it never reaches back into the board while another thread
    is mid-turn; call ``render`` when you want the current position.
    """

    def __init__(self, game: TicTacToeGame) -> None:
        self._game = game
        self._lock = threading.Lock()
        self._lines: list[str] = []
        game.subscribe(self)

    def on_event(self, event: GameEvent) -> None:
        with self._lock:
            self._lines.append(str(event))

    def transcript(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def render(self) -> str:
        status = self._game.status
        header = f"{self._game.current_player} to play" if status is GameStatus.IN_PROGRESS else str(status)
        return f"{header}\n{self._game.board.render()}"


# --8<-- [end:renderer]
