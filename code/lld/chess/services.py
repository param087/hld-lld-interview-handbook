"""The services: rule checking, the move history, and the game that owns both.

``MoveValidator`` answers "is this legal, and if not, why not". ``MoveHistory`` is the
Memento caretaker. ``ChessGame`` is the aggregate root: it owns the board, the lock and
the status machine, and it is the only object a UI or a network handler talks to.
"""

from __future__ import annotations

import threading

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock, ValidationError
from lld.chess.board import Board
from lld.chess.models import (
    Color,
    GameOverError,
    GameStatus,
    IllegalMoveError,
    Move,
    MoveRecord,
    NothingToUndoError,
    NotYourTurnError,
    PieceType,
    PlayedMove,
    Player,
    Square,
    parse_move_text,
)


# --8<-- [start:validator]
class MoveValidator:
    """Turns a raw (from, to) request into a legal ``Move`` or an error that names the reason.

    The last block is the one interviews are really about: a move can be geometrically
    perfect and still illegal because your own king ends up in check. There is no way to
    know without playing it, so the validator applies the move, asks, and takes it back.
    """

    def validate(
        self, board: Board, origin: Square, target: Square, promotion: PieceType | None = None
    ) -> Move:
        piece = board.piece_at(origin)
        if piece is None:
            raise IllegalMoveError(f"no piece on {origin}")
        if piece.color is not board.side_to_move:
            raise NotYourTurnError(
                f"{origin} holds a {piece.color} {piece.piece_type}; it is {board.side_to_move} to move"
            )
        candidates = [
            move
            for move in board.pseudo_legal_moves(piece.color)
            if move.origin == origin and move.target == target
        ]
        if not candidates:
            raise IllegalMoveError(f"a {piece.color} {piece.piece_type} cannot move {origin} to {target}")
        if candidates[0].promotion is not None:
            if promotion is None:
                raise IllegalMoveError(f"promotion on {target} needs a choice, e.g. {origin}{target}q")
            candidates = [move for move in candidates if move.promotion is promotion]
            if not candidates:
                raise IllegalMoveError(f"a pawn cannot promote to a {promotion}")
        move = candidates[0]

        record = board.apply(move)
        exposed = board.in_check(piece.color)
        board.undo(record)
        if exposed:
            raise IllegalMoveError(f"{move} leaves the {piece.color} king in check")
        return move


# --8<-- [end:validator]


# --8<-- [start:history]
class MoveHistory:
    """Caretaker: it stores mementos and never looks inside them."""

    def __init__(self) -> None:
        self._entries: list[PlayedMove] = []

    def push(self, record: MoveRecord, played_at: float) -> PlayedMove:
        entry = PlayedMove(record=record, played_at=played_at)
        self._entries.append(entry)
        return entry

    def pop(self) -> PlayedMove:
        if not self._entries:
            raise NothingToUndoError("no move to undo")
        return self._entries.pop()

    def entries(self) -> list[PlayedMove]:
        return list(self._entries)

    def notation(self) -> str:
        """``"1. e2e4 e7e5 2. f1c4"`` - coordinate notation, one number per full move."""
        parts: list[str] = []
        for index, entry in enumerate(self._entries):
            if index % 2 == 0:
                parts.append(f"{index // 2 + 1}.")
            parts.append(str(entry))
        return " ".join(parts)

    def __len__(self) -> int:
        return len(self._entries)


# --8<-- [end:history]


# --8<-- [start:game]
class ChessGame:
    """Aggregate root. One lock guards the board, the status and the history together.

    Move generation *mutates* the board (apply, ask, undo), so even ``legal_moves`` is a
    write as far as locking is concerned. That is the single most important sentence in
    this class: two threads reading moves at the same time would corrupt the position.
    """

    def __init__(
        self,
        white: Player,
        black: Player,
        board: Board | None = None,
        validator: MoveValidator | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        if white.color is not Color.WHITE or black.color is not Color.BLACK:
            raise ValidationError("white must play white and black must play black")
        self.id = (ids or SequentialIdGenerator("G")).next_id()
        self.players: dict[Color, Player] = {Color.WHITE: white, Color.BLACK: black}
        self._board = board if board is not None else Board.standard()
        self._validator = validator or MoveValidator()
        self._clock = clock or SystemClock()
        self._history = MoveHistory()
        self._lock = threading.RLock()
        self.status = GameStatus.ACTIVE
        self.winner: Color | None = None
        self.started_at = self._clock.now()
        self._refresh_status(self._board.side_to_move.opponent)

    @property
    def board(self) -> Board:
        return self._board

    @property
    def history(self) -> MoveHistory:
        return self._history

    @property
    def side_to_move(self) -> Color:
        with self._lock:
            return self._board.side_to_move

    def legal_moves(self) -> list[Move]:
        with self._lock:
            return self._board.legal_moves(self._board.side_to_move)

    def play(self, text: str) -> PlayedMove:
        """``game.play("e2e4")`` / ``game.play("a7a8q")``."""
        origin, target, promotion = parse_move_text(text)
        return self.make_move(origin, target, promotion)

    def make_move(
        self, origin: Square, target: Square, promotion: PieceType | None = None
    ) -> PlayedMove:
        with self._lock:
            if self.status.is_over:
                raise GameOverError(f"game {self.id} is over: {self.status}")
            mover = self._board.side_to_move
            move = self._validator.validate(self._board, origin, target, promotion)
            record = self._board.apply(move)
            played = self._history.push(record, self._clock.now())
            self._refresh_status(mover)
            return played

    def undo(self) -> PlayedMove:
        """Take back the last move, captured piece and castling rights included."""
        with self._lock:
            if self.status in (GameStatus.RESIGNED, GameStatus.DRAW):
                raise GameOverError(f"game {self.id} ended by agreement; undo would rewrite the result")
            played = self._history.pop()
            self._board.undo(played.record)
            self._refresh_status(self._board.side_to_move.opponent)
            return played

    def resign(self, color: Color) -> None:
        with self._lock:
            self._require_running()
            self.status = GameStatus.RESIGNED
            self.winner = color.opponent

    def agree_draw(self) -> None:
        with self._lock:
            self._require_running()
            self.status = GameStatus.DRAW
            self.winner = None

    def result(self) -> str:
        with self._lock:
            if self.status is GameStatus.CHECKMATE and self.winner is not None:
                return f"{self.players[self.winner].name} wins by checkmate"
            if self.status is GameStatus.RESIGNED and self.winner is not None:
                return f"{self.players[self.winner].name} wins by resignation"
            if self.status is GameStatus.STALEMATE:
                return "draw by stalemate"
            if self.status is GameStatus.DRAW:
                return "draw by agreement"
            return f"in progress ({self.status})"

    def _require_running(self) -> None:
        if self.status.is_over:
            raise GameOverError(f"game {self.id} is over: {self.status}")

    def _refresh_status(self, mover: Color) -> None:
        """Recompute the status of the position ``mover`` just created (or just undid)."""
        opponent = mover.opponent
        in_check = self._board.in_check(opponent)
        if not self._board.legal_moves(opponent):
            self.status = GameStatus.CHECKMATE if in_check else GameStatus.STALEMATE
            self.winner = mover if in_check else None
        else:
            self.status = GameStatus.CHECK if in_check else GameStatus.ACTIVE
            self.winner = None


# --8<-- [end:game]
