"""Enums, value objects, the move memento and domain errors for chess.

Everything here is immutable. The mutable position lives in ``board.py`` and the
services that drive it live in ``services.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError

if TYPE_CHECKING:  # pragma: no cover - only needed for the annotation on MoveRecord
    from lld.chess.pieces import Piece

FILES = "abcdefgh"
RANKS = "12345678"


# --8<-- [start:enums]
class Color(StrEnum):
    WHITE = "white"
    BLACK = "black"

    @property
    def opponent(self) -> Color:
        return Color.BLACK if self is Color.WHITE else Color.WHITE


class PieceType(StrEnum):
    KING = "king"
    QUEEN = "queen"
    ROOK = "rook"
    BISHOP = "bishop"
    KNIGHT = "knight"
    PAWN = "pawn"


class MoveKind(StrEnum):
    """What the board has to do beyond "pick the piece up and put it down"."""

    NORMAL = "normal"
    DOUBLE_PUSH = "double_push"  # sets the en-passant target square
    EN_PASSANT = "en_passant"  # captures a pawn that is not on the target square
    CASTLE_KING_SIDE = "castle_king_side"  # moves the rook too
    CASTLE_QUEEN_SIDE = "castle_queen_side"


class GameStatus(StrEnum):
    ACTIVE = "active"
    CHECK = "check"
    CHECKMATE = "checkmate"
    STALEMATE = "stalemate"
    DRAW = "draw"
    RESIGNED = "resigned"

    @property
    def is_over(self) -> bool:
        return self in (
            GameStatus.CHECKMATE,
            GameStatus.STALEMATE,
            GameStatus.DRAW,
            GameStatus.RESIGNED,
        )


PROMOTION_LETTERS: dict[PieceType, str] = {
    PieceType.QUEEN: "q",
    PieceType.ROOK: "r",
    PieceType.BISHOP: "b",
    PieceType.KNIGHT: "n",
}
PROMOTION_CHOICES: tuple[PieceType, ...] = tuple(PROMOTION_LETTERS)
# --8<-- [end:enums]


# --8<-- [start:errors]
class IllegalMoveError(ValidationError):
    """The move breaks a rule: wrong geometry, blocked path, or it leaves your king in check."""


class NotYourTurnError(ConflictError):
    """The piece belongs to the other side, or the other side is to move."""


class GameOverError(InvalidStateError):
    """The game already ended; no further moves, resignations or draws are accepted."""


class NothingToUndoError(NotFoundError):
    """Undo was asked for on an empty move history."""


# --8<-- [end:errors]


# --8<-- [start:values]
@dataclass(frozen=True, slots=True, order=True)
class Square:
    """A board coordinate. ``file`` is a-h as 0-7, ``rank`` is 1-8 as 0-7."""

    file: int
    rank: int

    def __post_init__(self) -> None:
        if not (0 <= self.file <= 7 and 0 <= self.rank <= 7):
            raise ValidationError(f"square off the board: file={self.file} rank={self.rank}")

    @classmethod
    def of(cls, name: str) -> Square:
        text = name.strip().lower()
        if len(text) != 2 or text[0] not in FILES or text[1] not in RANKS:
            raise ValidationError(f"not a square name: {name!r}")
        return cls(FILES.index(text[0]), RANKS.index(text[1]))

    @property
    def name(self) -> str:
        return f"{FILES[self.file]}{RANKS[self.rank]}"

    def shifted(self, files: int, ranks: int) -> Square | None:
        """The square this many steps away, or ``None`` when that falls off the board."""
        file, rank = self.file + files, self.rank + ranks
        return Square(file, rank) if 0 <= file <= 7 and 0 <= rank <= 7 else None

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Move:
    """A Command object: what to do, not how. ``kind`` carries the three special cases."""

    origin: Square
    target: Square
    kind: MoveKind = MoveKind.NORMAL
    promotion: PieceType | None = None

    @property
    def is_castle(self) -> bool:
        return self.kind in (MoveKind.CASTLE_KING_SIDE, MoveKind.CASTLE_QUEEN_SIDE)

    def __str__(self) -> str:
        return f"{self.origin}{self.target}{PROMOTION_LETTERS[self.promotion] if self.promotion else ''}"


@dataclass(frozen=True, slots=True)
class CastlingRights:
    """Four booleans that only ever go from True to False within a game."""

    white_king_side: bool = True
    white_queen_side: bool = True
    black_king_side: bool = True
    black_queen_side: bool = True

    @staticmethod
    def _field(color: Color, king_side: bool) -> str:
        return f"{color.value}_{'king' if king_side else 'queen'}_side"

    def allows(self, color: Color, king_side: bool) -> bool:
        return bool(getattr(self, self._field(color, king_side)))

    def revoked(self, color: Color, king_side: bool | None = None) -> CastlingRights:
        """A new value with one side (or both, when ``king_side`` is None) switched off."""
        sides = (True, False) if king_side is None else (king_side,)
        return replace(self, **{self._field(color, side): False for side in sides})

    def __str__(self) -> str:
        flags = "".join(
            letter
            for letter, on in zip(
                "KQkq",
                (
                    self.white_king_side,
                    self.white_queen_side,
                    self.black_king_side,
                    self.black_queen_side,
                ),
                strict=True,
            )
            if on
        )
        return flags or "-"


@dataclass(frozen=True, slots=True)
class MoveRecord:
    """Memento. Everything ``Board.undo`` needs to restore the position exactly.

    The captured piece and the previous castling rights are the two fields
    candidates forget; without them undo silently corrupts the game.
    """

    move: Move
    moved: Piece
    captured: Piece | None
    captured_square: Square | None
    previous_castling: CastlingRights
    previous_en_passant: Square | None
    previous_halfmove_clock: int


@dataclass(frozen=True, slots=True)
class Player:
    name: str
    color: Color


@dataclass(frozen=True, slots=True)
class PlayedMove:
    """One entry in the move history: the memento plus when it was played."""

    record: MoveRecord
    played_at: float

    @property
    def move(self) -> Move:
        return self.record.move

    @property
    def color(self) -> Color:
        return self.record.moved.color

    def __str__(self) -> str:
        return str(self.record.move)


def parse_move_text(text: str) -> tuple[Square, Square, PieceType | None]:
    """Coordinate notation: ``"e2e4"``, or ``"a7a8q"`` for a promotion."""
    cleaned = text.strip().lower().replace("-", "")
    if len(cleaned) not in (4, 5):
        raise ValidationError(f"not a move: {text!r} (expected e2e4 or a7a8q)")
    promotion: PieceType | None = None
    if len(cleaned) == 5:
        letters = {letter: piece for piece, letter in PROMOTION_LETTERS.items()}
        if cleaned[4] not in letters:
            raise ValidationError(f"cannot promote to {cleaned[4]!r}; use one of qrbn")
        promotion = letters[cleaned[4]]
    return Square.of(cleaned[:2]), Square.of(cleaned[2:4]), promotion


# --8<-- [end:values]
