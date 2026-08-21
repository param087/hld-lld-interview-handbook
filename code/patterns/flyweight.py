"""Flyweight: share the part of an object that never changes, pass in the part that does.

The running example is a text editor. A document is a long run of characters
and each character is drawn in some style. ``TextStyle`` (font, size, weight,
colour) is intrinsic state: immutable and shared. ``Glyph`` pairs one character
with one style and is intrinsic too, so the letter ``e`` in 12pt bold exists
once however many times it is typed. Where a glyph sits on the page is
extrinsic: ``Document`` computes the position at layout time and passes it to
``Glyph.draw``. ``GlyphFactory`` is the pool that makes sharing happen: equal
arguments return the identical object. The second half shows the Pythonic
forms: ``functools.cache`` on a constructor function and ``Enum`` members as
flyweights, with a chess board that holds 32 squares and 12 piece objects.
"""

from __future__ import annotations

import re
import sys
import threading
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, dataclass
from enum import Enum, StrEnum
from functools import cache
from itertools import islice

from common import ValidationError


# --8<-- [start:flyweight]
@dataclass(frozen=True, slots=True)
class TextStyle:
    """Intrinsic state: immutable, so one instance can be shared by every glyph that uses it."""

    font: str
    size: int
    bold: bool = False
    italic: bool = False
    colour: str = "black"

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValidationError("font size must be positive")

    def __str__(self) -> str:
        flags = ("b" if self.bold else "") + ("i" if self.italic else "")
        return f"{self.font} {self.size}{flags} {self.colour}"


@dataclass(frozen=True, slots=True)
class Glyph:
    """The Flyweight: one character in one style. Everything about *where* it goes is passed in."""

    char: str
    style: TextStyle

    def draw(self, row: int, col: int) -> str:
        """The position is extrinsic: it arrives as arguments and is never stored on the glyph."""
        return f"{self.char!r} at ({row},{col}) in {self.style}"


class GlyphFactory:
    """The Flyweight Factory: the pool that turns equal arguments into the identical object.

    The lock makes check-then-create atomic. Without it two threads could each
    build a glyph for the same key, and one of them would keep an unshared copy
    that nothing else ever sees again.
    """

    def __init__(self) -> None:
        self._pool: dict[tuple[str, TextStyle], Glyph] = {}
        self._lock = threading.Lock()

    def get(self, char: str, style: TextStyle) -> Glyph:
        if len(char) != 1:
            raise ValidationError("a glyph is exactly one character")
        key = (char, style)
        with self._lock:
            glyph = self._pool.get(key)
            if glyph is None:
                glyph = self._pool[key] = Glyph(char, style)
            return glyph

    def __len__(self) -> int:
        return len(self._pool)


# --8<-- [end:flyweight]


# --8<-- [start:document]
class Document:
    """The client: a run of shared glyphs. Positions are computed, never stored per character."""

    def __init__(self, factory: GlyphFactory, width: int = 40) -> None:
        if width <= 0:
            raise ValidationError("width must be positive")
        self._factory = factory
        self._glyphs: list[Glyph] = []
        self._width = width

    def insert(self, text: str, style: TextStyle) -> None:
        self._glyphs.extend(self._factory.get(char, style) for char in text)

    def __len__(self) -> int:
        return len(self._glyphs)

    def glyph_at(self, index: int) -> Glyph:
        return self._glyphs[index]

    def distinct_glyphs(self) -> int:
        return len({id(glyph) for glyph in self._glyphs})

    def layout(self) -> Iterator[tuple[int, int, Glyph]]:
        """Extrinsic state on the fly: row and column follow from the index and the width."""
        for index, glyph in enumerate(self._glyphs):
            row, col = divmod(index, self._width)
            yield row, col, glyph

    def draw(self, start: int = 0, count: int = 3) -> list[str]:
        return [glyph.draw(row, col) for row, col, glyph in islice(self.layout(), start, start + count)]


# --8<-- [end:document]


# --8<-- [start:pythonic]
class Colour(StrEnum):
    WHITE = "white"
    BLACK = "black"


class PieceKind(Enum):
    """Enum members are flyweights by construction: one object per member, shared by every reference."""

    KING = ("K", 0)
    QUEEN = ("Q", 9)
    ROOK = ("R", 5)
    BISHOP = ("B", 3)
    KNIGHT = ("N", 3)
    PAWN = ("P", 1)

    def __init__(self, symbol: str, points: int) -> None:
        self.symbol = symbol
        self.points = points


@dataclass(frozen=True, slots=True)
class Piece:
    """Intrinsic: kind and colour. The square it stands on belongs to the board, not to the piece."""

    kind: PieceKind
    colour: Colour

    @property
    def symbol(self) -> str:
        return self.kind.symbol if self.colour is Colour.WHITE else self.kind.symbol.lower()


@cache
def piece(kind: PieceKind, colour: Colour) -> Piece:
    """``functools.cache`` on a constructor function is a flyweight factory in one line."""
    return Piece(kind, colour)


def starting_board() -> dict[str, Piece]:
    """32 squares, 12 objects: the board maps extrinsic squares to intrinsic pieces."""
    back_rank = (
        PieceKind.ROOK, PieceKind.KNIGHT, PieceKind.BISHOP, PieceKind.QUEEN,
        PieceKind.KING, PieceKind.BISHOP, PieceKind.KNIGHT, PieceKind.ROOK,
    )
    board: dict[str, Piece] = {}
    for file, kind in zip("abcdefgh", back_rank, strict=True):
        board[f"{file}1"] = piece(kind, Colour.WHITE)
        board[f"{file}2"] = piece(PieceKind.PAWN, Colour.WHITE)
        board[f"{file}7"] = piece(PieceKind.PAWN, Colour.BLACK)
        board[f"{file}8"] = piece(kind, Colour.BLACK)
    return board


# --8<-- [end:pythonic]


def main() -> None:
    factory = GlyphFactory()
    body = TextStyle("Helvetica", 12)
    heading = TextStyle("Helvetica", 18, bold=True)
    emphasis = TextStyle("Helvetica", 12, italic=True, colour="red")
    doc = Document(factory, width=40)
    doc.insert("Flyweight", heading)
    doc.insert("the quick brown fox jumps over the lazy dog " * 20, body)
    doc.insert("lazy" * 5, emphasis)

    print("--- a document of hundreds of characters, built from a few dozen shared objects ---")
    print(f"characters: {len(doc)}, glyph objects: {doc.distinct_glyphs()}, pool size: {len(factory)}")
    first_e, later_e = doc.glyph_at(11), doc.glyph_at(11 + 44)
    print(f"the 'e' of 'the' on line one and on line two: same object -> {first_e is later_e}")

    print("--- extrinsic state: the position is computed at layout time and passed in ---")
    for line in doc.draw(start=0, count=2) + doc.draw(start=40, count=1):
        print(line)

    print("--- shared objects must be immutable, and the dataclass enforces it ---")
    try:
        first_e.char = "x"  # type: ignore[misc]
    except FrozenInstanceError as exc:
        print(f"FrozenInstanceError: {exc}")

    print("--- chess: 32 squares on the board, 12 piece objects in memory ---")
    board = starting_board()
    print(f"squares: {len(board)}, objects: {len({id(p) for p in board.values()})}, "
          f"cache: {piece.cache_info().currsize}")
    print(f"e2 and d2 share one white pawn: {board['e2'] is board['d2']}; "
          f"symbols: {''.join(board[f'{f}8'].symbol for f in 'abcdefgh')}")

    print("--- the interpreter does it too: small ints, interned strings, compiled patterns ---")
    print(f"int('256') is int('256'): {int('256') is int('256')}; "
          f"int('257') is int('257'): {int('257') is int('257')}")
    built_a, built_b = "".join(["fly", "weight"]), "".join(["fly", "weight"])
    print(f"two built strings: {built_a is built_b}; "
          f"after sys.intern: {sys.intern(built_a) is sys.intern(built_b)}")
    print(f"re.compile returns the cached pattern: {re.compile('[a-z]+') is re.compile('[a-z]+')}")


if __name__ == "__main__":
    main()
