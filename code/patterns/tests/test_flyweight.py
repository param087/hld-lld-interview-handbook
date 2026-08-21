"""Flyweight: equal arguments give the identical object, shared objects are immutable, positions stay outside."""

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from common import ValidationError
from patterns.flyweight import (
    Colour,
    Document,
    Glyph,
    GlyphFactory,
    Piece,
    PieceKind,
    TextStyle,
    piece,
    starting_board,
)

BODY = TextStyle("Helvetica", 12)
BOLD = TextStyle("Helvetica", 12, bold=True)


def test_factory_returns_the_identical_object_for_equal_arguments() -> None:
    factory = GlyphFactory()
    first = factory.get("e", BODY)
    assert factory.get("e", TextStyle("Helvetica", 12)) is first  # equal style value, same glyph
    assert factory.get("e", BOLD) is not first
    assert factory.get("f", BODY) is not first
    assert len(factory) == 3
    assert Glyph("e", BODY) == first and Glyph("e", BODY) is not first  # equality is free; identity needs the pool


def test_document_shares_glyphs_and_computes_positions_instead_of_storing_them() -> None:
    factory = GlyphFactory()
    doc = Document(factory, width=10)
    doc.insert("abcabcabc", BODY)
    doc.insert("abc", BOLD)
    assert len(doc) == 12
    assert doc.distinct_glyphs() == 6 and len(factory) == 6
    assert doc.glyph_at(0) is doc.glyph_at(3) is doc.glyph_at(6)
    assert doc.glyph_at(0) is not doc.glyph_at(9)  # same character, different style
    positions = [(row, col, glyph.char) for row, col, glyph in doc.layout()]
    assert positions[:3] == [(0, 0, "a"), (0, 1, "b"), (0, 2, "c")]
    assert positions[10:] == [(1, 0, "b"), (1, 1, "c")]
    assert doc.draw(start=10, count=1) == ["'b' at (1,0) in Helvetica 12b black"]


def test_flyweights_are_immutable_and_validated() -> None:
    glyph = GlyphFactory().get("e", BODY)
    with pytest.raises(FrozenInstanceError):
        glyph.char = "x"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        glyph.style.size = 99  # type: ignore[misc]
    with pytest.raises(ValidationError):
        TextStyle("Helvetica", 0)
    with pytest.raises(ValidationError):
        GlyphFactory().get("ab", BODY)
    with pytest.raises(ValidationError):
        Document(GlyphFactory(), width=0)


def test_concurrent_callers_never_get_two_objects_for_one_key() -> None:
    factory = GlyphFactory()
    chars = "abcde"

    def fetch(worker: int) -> list[int]:
        return [id(factory.get(chars[(worker + k) % len(chars)], BODY)) for k in range(200)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        seen = {object_id for ids in pool.map(fetch, range(16)) for object_id in ids}
    assert len(seen) == len(chars) and len(factory) == len(chars)


def test_enum_members_and_a_cached_constructor_are_flyweights() -> None:
    board = starting_board()
    assert len(board) == 32
    assert len({id(p) for p in board.values()}) == 12
    assert board["e2"] is board["d2"] is piece(PieceKind.PAWN, Colour.WHITE)
    assert board["e1"] is piece(PieceKind.KING, Colour.WHITE) and board["e1"].symbol == "K"
    assert board["e8"].symbol == "k" and board["e8"].kind is PieceKind.KING
    assert "".join(board[f"{f}1"].symbol for f in "abcdefgh") == "RNBQKBNR"
    assert Piece(PieceKind.QUEEN, Colour.BLACK) == piece(PieceKind.QUEEN, Colour.BLACK)
    assert sum(p.kind.points for p in board.values()) == 2 * (9 + 2 * 5 + 2 * 3 + 2 * 3 + 8)


def test_the_interpreter_interns_strings_and_caches_compiled_patterns() -> None:
    built_a, built_b = "".join(["fly", "weight"]), "".join(["fly", "weight"])
    assert built_a == built_b and built_a is not built_b
    assert sys.intern(built_a) is sys.intern(built_b)
    assert re.compile("[a-z]+") is re.compile("[a-z]+")
