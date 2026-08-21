"""The position: piece placement, the attack test, and the apply/undo pair.

``Board`` is the Memento *originator*: ``apply`` returns a ``MoveRecord`` that
``undo`` consumes to restore the position bit for bit. Nothing here knows about
turns, players or results - that is ``services.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from common import NotFoundError
from lld.chess.models import (
    CastlingRights,
    Color,
    IllegalMoveError,
    Move,
    MoveKind,
    MoveRecord,
    PieceType,
    Square,
)
from lld.chess.pieces import Piece, PieceFactory

BACK_RANK: tuple[PieceType, ...] = (
    PieceType.ROOK,
    PieceType.KNIGHT,
    PieceType.BISHOP,
    PieceType.QUEEN,
    PieceType.KING,
    PieceType.BISHOP,
    PieceType.KNIGHT,
    PieceType.ROOK,
)


# --8<-- [start:board]
class Board:
    """Squares plus the four pieces of state a position needs beyond piece placement."""

    __slots__ = ("_squares", "side_to_move", "castling", "en_passant", "halfmove_clock")

    # A rook standing here means the matching castling right may still be alive.
    _ROOK_HOMES: ClassVar[dict[Square, tuple[Color, bool]]] = {
        Square(0, 0): (Color.WHITE, False),
        Square(7, 0): (Color.WHITE, True),
        Square(0, 7): (Color.BLACK, False),
        Square(7, 7): (Color.BLACK, True),
    }

    def __init__(
        self,
        squares: Mapping[Square, Piece],
        side_to_move: Color = Color.WHITE,
        castling: CastlingRights | None = None,
        en_passant: Square | None = None,
        halfmove_clock: int = 0,
    ) -> None:
        self._squares: dict[Square, Piece] = dict(squares)
        self.side_to_move = side_to_move
        self.castling = castling if castling is not None else CastlingRights()
        self.en_passant = en_passant
        self.halfmove_clock = halfmove_clock

    @classmethod
    def standard(cls) -> Board:
        """Factory: the opening position, built from the flyweight pool."""
        squares: dict[Square, Piece] = {}
        for file, piece_type in enumerate(BACK_RANK):
            squares[Square(file, 0)] = PieceFactory.create(Color.WHITE, piece_type)
            squares[Square(file, 7)] = PieceFactory.create(Color.BLACK, piece_type)
            squares[Square(file, 1)] = PieceFactory.create(Color.WHITE, PieceType.PAWN)
            squares[Square(file, 6)] = PieceFactory.create(Color.BLACK, PieceType.PAWN)
        return cls(squares)

    @classmethod
    def from_placement(
        cls,
        placement: Mapping[str, str],
        side_to_move: Color = Color.WHITE,
        castling: CastlingRights | None = None,
        en_passant: str | None = None,
    ) -> Board:
        """``Board.from_placement({"e1": "K", "e8": "k", "a7": "P"})`` - for tests and puzzles."""
        squares = {Square.of(name): PieceFactory.from_symbol(sym) for name, sym in placement.items()}
        return cls(
            squares,
            side_to_move,
            castling if castling is not None else CastlingRights(False, False, False, False),
            Square.of(en_passant) if en_passant else None,
        )

    # -- reading the position ---------------------------------------------------------
    def piece_at(self, square: Square) -> Piece | None:
        return self._squares.get(square)

    def occupied(self) -> list[tuple[Square, Piece]]:
        return sorted(self._squares.items())

    def pieces_of(self, color: Color) -> list[tuple[Square, Piece]]:
        return [(square, piece) for square, piece in self._squares.items() if piece.color is color]

    def king_square(self, color: Color) -> Square:
        for square, piece in self._squares.items():
            if piece.piece_type is PieceType.KING and piece.color is color:
                return square
        raise NotFoundError(f"no {color} king on the board")

    def is_attacked(self, square: Square, by: Color) -> bool:
        """Ask every enemy piece whether it hits this square. O(pieces), no recursion."""
        return any(piece.attacks(self, origin, square) for origin, piece in self.pieces_of(by))

    def in_check(self, color: Color) -> bool:
        return self.is_attacked(self.king_square(color), color.opponent)

    # -- generating moves -------------------------------------------------------------
    def pseudo_legal_moves(self, color: Color) -> list[Move]:
        moves: list[Move] = []
        for origin, piece in self.pieces_of(color):
            moves.extend(piece.candidate_moves(self, origin))
        return moves

    def legal_moves(self, color: Color | None = None) -> list[Move]:
        """Pseudo-legal moves minus the ones that leave your own king in check.

        This is the pin rule, and it is why the board must be *simulated*: there is no
        cheap static test that catches a knight pinned against its king.
        """
        color = color if color is not None else self.side_to_move
        legal: list[Move] = []
        for move in self.pseudo_legal_moves(color):
            record = self.apply(move)
            if not self.in_check(color):
                legal.append(move)
            self.undo(record)
        return legal

    # -- mutating the position --------------------------------------------------------
    def apply(self, move: Move) -> MoveRecord:
        """Play the move and return the memento that undoes it."""
        piece = self._squares.get(move.origin)
        if piece is None:
            raise IllegalMoveError(f"no piece on {move.origin}")
        captured_square = move.target
        if move.kind is MoveKind.EN_PASSANT:
            captured_square = Square(move.target.file, move.origin.rank)
        captured = self._squares.pop(captured_square, None)
        record = MoveRecord(
            move=move,
            moved=piece,
            captured=captured,
            captured_square=captured_square if captured is not None else None,
            previous_castling=self.castling,
            previous_en_passant=self.en_passant,
            previous_halfmove_clock=self.halfmove_clock,
        )
        del self._squares[move.origin]
        landing = piece if move.promotion is None else PieceFactory.create(piece.color, move.promotion)
        self._squares[move.target] = landing
        if move.is_castle:
            rook_from, rook_to = self._rook_squares(move)
            self._squares[rook_to] = self._squares.pop(rook_from)
        self.castling = self._castling_after(move, piece, captured, captured_square)
        self.en_passant = (
            Square(move.origin.file, (move.origin.rank + move.target.rank) // 2)
            if move.kind is MoveKind.DOUBLE_PUSH
            else None
        )
        reset = captured is not None or piece.piece_type is PieceType.PAWN
        self.halfmove_clock = 0 if reset else self.halfmove_clock + 1
        self.side_to_move = piece.color.opponent
        return record

    def undo(self, record: MoveRecord) -> None:
        """Restore the position the memento came from - captures and rights included."""
        move = record.move
        self._squares.pop(move.target, None)
        self._squares[move.origin] = record.moved
        if record.captured is not None and record.captured_square is not None:
            self._squares[record.captured_square] = record.captured
        if move.is_castle:
            rook_from, rook_to = self._rook_squares(move)
            self._squares[rook_from] = self._squares.pop(rook_to)
        self.castling = record.previous_castling
        self.en_passant = record.previous_en_passant
        self.halfmove_clock = record.previous_halfmove_clock
        self.side_to_move = record.moved.color

    def _castling_after(
        self, move: Move, piece: Piece, captured: Piece | None, captured_square: Square
    ) -> CastlingRights:
        rights = self.castling
        if piece.piece_type is PieceType.KING:
            rights = rights.revoked(piece.color)
        elif piece.piece_type is PieceType.ROOK and move.origin in self._ROOK_HOMES:
            color, king_side = self._ROOK_HOMES[move.origin]
            if color is piece.color:
                rights = rights.revoked(color, king_side)
        if captured is not None and captured.piece_type is PieceType.ROOK:
            home = self._ROOK_HOMES.get(captured_square)
            if home is not None and home[0] is captured.color:
                rights = rights.revoked(home[0], home[1])
        return rights

    @staticmethod
    def _rook_squares(move: Move) -> tuple[Square, Square]:
        rank = move.origin.rank
        if move.kind is MoveKind.CASTLE_KING_SIDE:
            return Square(7, rank), Square(5, rank)
        return Square(0, rank), Square(3, rank)

    def row(self, rank: int) -> str:
        """One rank as eight characters, ``"."`` for an empty square."""
        cells = (self._squares.get(Square(file, rank)) for file in range(8))
        return "".join(cell.symbol if cell else "." for cell in cells)

    def render(self) -> str:
        return "\n".join([f"{rank + 1} {self.row(rank)}" for rank in range(7, -1, -1)] + ["  abcdefgh"])


# --8<-- [end:board]
