"""The piece hierarchy: move generation by polymorphism, never by a type switch.

``Piece.candidate_moves`` is a Template Method. The skeleton (walk the squares this
piece can reach, drop the ones holding a friendly piece, wrap the rest in ``Move``
objects) is written once; subclasses fill in three hooks:

* ``_target_squares`` - the geometry (sliding rays, fixed offsets, pawn pushes),
* ``_moves_to`` - decoration of a single move (only the pawn needs it: promotion),
* ``_extra_moves`` - moves that are not "go to a reachable square" (castling).

Pieces carry no per-square state, so one instance per (colour, type) is shared by
every square that holds one: a Flyweight with 12 objects for 32 pieces.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import ClassVar, Protocol

from common import ValidationError
from lld.chess.models import (
    PROMOTION_CHOICES,
    CastlingRights,
    Color,
    Move,
    MoveKind,
    PieceType,
    Square,
)

DIAGONALS: tuple[tuple[int, int], ...] = ((1, 1), (1, -1), (-1, 1), (-1, -1))
ORTHOGONALS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))


# --8<-- [start:base]
class BoardView(Protocol):
    """All a piece is allowed to know about the position while generating moves."""

    castling: CastlingRights
    en_passant: Square | None

    def piece_at(self, square: Square) -> Piece | None: ...

    def is_attacked(self, square: Square, by: Color) -> bool: ...


class Piece(ABC):
    """Immutable and shared. ``candidate_moves`` is the Template Method."""

    __slots__ = ("color",)

    piece_type: ClassVar[PieceType]
    letter: ClassVar[str]

    def __init__(self, color: Color) -> None:
        self.color = color

    def candidate_moves(self, board: BoardView, origin: Square) -> list[Move]:
        """Pseudo-legal moves: correct geometry, but they may leave the king in check."""
        moves: list[Move] = []
        for target in self._target_squares(board, origin):
            occupant = board.piece_at(target)
            if occupant is not None and occupant.color is self.color:
                continue
            moves.extend(self._moves_to(board, origin, target))
        moves.extend(self._extra_moves(board, origin))
        return moves

    def attacks(self, board: BoardView, origin: Square, target: Square) -> bool:
        """Does this piece hit ``target`` from ``origin``? Used by the check test.

        Deliberately not ``target in candidate_moves``: castling calls ``is_attacked``,
        so asking the king for its moves here would recurse forever.
        """
        return any(square == target for square in self._target_squares(board, origin))

    @abstractmethod
    def _target_squares(self, board: BoardView, origin: Square) -> Iterator[Square]:
        """Squares this piece reaches, ignoring whose piece stands there."""

    def _moves_to(self, board: BoardView, origin: Square, target: Square) -> Iterator[Move]:
        yield Move(origin, target)

    def _extra_moves(self, board: BoardView, origin: Square) -> Iterator[Move]:
        return iter(())

    @property
    def symbol(self) -> str:
        return self.letter.upper() if self.color is Color.WHITE else self.letter.lower()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.color.value})"


class SlidingPiece(Piece):
    """Queen, rook, bishop: walk each ray until something blocks it."""

    __slots__ = ()

    directions: ClassVar[tuple[tuple[int, int], ...]]

    def _target_squares(self, board: BoardView, origin: Square) -> Iterator[Square]:
        for files, ranks in self.directions:
            square = origin.shifted(files, ranks)
            while square is not None:
                yield square
                if board.piece_at(square) is not None:
                    break  # the blocker itself is a target; anything behind it is not
                square = square.shifted(files, ranks)


class SteppingPiece(Piece):
    """Knight and king: a fixed set of one-shot offsets."""

    __slots__ = ()

    offsets: ClassVar[tuple[tuple[int, int], ...]]

    def _target_squares(self, board: BoardView, origin: Square) -> Iterator[Square]:
        for files, ranks in self.offsets:
            square = origin.shifted(files, ranks)
            if square is not None:
                yield square


# --8<-- [end:base]


# --8<-- [start:pieces]
class Queen(SlidingPiece):
    __slots__ = ()
    piece_type = PieceType.QUEEN
    letter = "Q"
    directions = ORTHOGONALS + DIAGONALS


class Rook(SlidingPiece):
    __slots__ = ()
    piece_type = PieceType.ROOK
    letter = "R"
    directions = ORTHOGONALS


class Bishop(SlidingPiece):
    __slots__ = ()
    piece_type = PieceType.BISHOP
    letter = "B"
    directions = DIAGONALS


class Knight(SteppingPiece):
    __slots__ = ()
    piece_type = PieceType.KNIGHT
    letter = "N"
    offsets = ((1, 2), (2, 1), (2, -1), (1, -2), (-1, -2), (-2, -1), (-2, 1), (-1, 2))


class King(SteppingPiece):
    """The only piece with a move that is not "go to a reachable square"."""

    __slots__ = ()
    piece_type = PieceType.KING
    letter = "K"
    offsets = ORTHOGONALS + DIAGONALS

    # (king side, kind, files that must be empty, files the king crosses, landing file)
    _CASTLE_PLANS: ClassVar[tuple[tuple[bool, MoveKind, tuple[int, ...], tuple[int, ...], int], ...]] = (
        (True, MoveKind.CASTLE_KING_SIDE, (5, 6), (5, 6), 6),
        (False, MoveKind.CASTLE_QUEEN_SIDE, (1, 2, 3), (3, 2), 2),
    )

    def _extra_moves(self, board: BoardView, origin: Square) -> Iterator[Move]:
        rank = 0 if self.color is Color.WHITE else 7
        if origin != Square(4, rank) or board.is_attacked(origin, self.color.opponent):
            return  # you may not castle out of check
        for king_side, kind, empty_files, crossed_files, landing in self._CASTLE_PLANS:
            if not board.castling.allows(self.color, king_side):
                continue
            rook = board.piece_at(Square(7 if king_side else 0, rank))
            if rook is None or rook.piece_type is not PieceType.ROOK or rook.color is not self.color:
                continue
            if any(board.piece_at(Square(file, rank)) is not None for file in empty_files):
                continue
            if any(
                board.is_attacked(Square(file, rank), self.color.opponent)
                for file in crossed_files
            ):
                continue  # you may not castle through or into check
            yield Move(origin, Square(landing, rank), kind)


class Pawn(Piece):
    """Pushes forward, captures sideways, and is the only piece that becomes another."""

    __slots__ = ()
    piece_type = PieceType.PAWN
    letter = "P"

    @property
    def _forward(self) -> int:
        return 1 if self.color is Color.WHITE else -1

    @property
    def _start_rank(self) -> int:
        return 1 if self.color is Color.WHITE else 6

    @property
    def _last_rank(self) -> int:
        return 7 if self.color is Color.WHITE else 0

    def _target_squares(self, board: BoardView, origin: Square) -> Iterator[Square]:
        step = origin.shifted(0, self._forward)
        if step is not None and board.piece_at(step) is None:
            yield step
            double = origin.shifted(0, 2 * self._forward)
            if origin.rank == self._start_rank and double is not None and board.piece_at(double) is None:
                yield double
        for square in self._capture_squares(origin):
            if board.piece_at(square) is not None or square == board.en_passant:
                yield square

    def _capture_squares(self, origin: Square) -> Iterator[Square]:
        for files in (-1, 1):
            square = origin.shifted(files, self._forward)
            if square is not None:
                yield square

    def attacks(self, board: BoardView, origin: Square, target: Square) -> bool:
        # A pawn does not attack the square in front of it, so the generic
        # "reachable == attacked" rule would wrongly forbid a king from stepping there.
        return any(square == target for square in self._capture_squares(origin))

    def _moves_to(self, board: BoardView, origin: Square, target: Square) -> Iterator[Move]:
        if target.rank == self._last_rank:
            for promotion in PROMOTION_CHOICES:
                yield Move(origin, target, MoveKind.NORMAL, promotion)
        elif abs(target.rank - origin.rank) == 2:
            yield Move(origin, target, MoveKind.DOUBLE_PUSH)
        elif target.file != origin.file and board.piece_at(target) is None:
            yield Move(origin, target, MoveKind.EN_PASSANT)
        else:
            yield Move(origin, target)


# --8<-- [end:pieces]


# --8<-- [start:factory]
_PIECE_CLASSES: dict[PieceType, type[Piece]] = {
    PieceType.KING: King,
    PieceType.QUEEN: Queen,
    PieceType.ROOK: Rook,
    PieceType.BISHOP: Bishop,
    PieceType.KNIGHT: Knight,
    PieceType.PAWN: Pawn,
}
_LETTERS: dict[str, PieceType] = {klass.letter: kind for kind, klass in _PIECE_CLASSES.items()}
# Flyweight pool, interned once at import: 2 colours x 6 types = 12 objects, forever.
_INTERNED: dict[tuple[Color, PieceType], Piece] = {
    (color, kind): klass(color) for kind, klass in _PIECE_CLASSES.items() for color in Color
}


class PieceFactory:
    """Factory + Flyweight: every ``e2`` pawn on every board is the same object."""

    @staticmethod
    def create(color: Color, piece_type: PieceType) -> Piece:
        return _INTERNED[(color, piece_type)]

    @staticmethod
    def from_symbol(symbol: str) -> Piece:
        """``"K"`` is a white king, ``"k"`` a black one - the usual FEN letters."""
        try:
            piece_type = _LETTERS[symbol.upper()]
        except KeyError:
            raise ValidationError(f"unknown piece symbol {symbol!r}") from None
        return PieceFactory.create(Color.WHITE if symbol.isupper() else Color.BLACK, piece_type)


# --8<-- [end:factory]
