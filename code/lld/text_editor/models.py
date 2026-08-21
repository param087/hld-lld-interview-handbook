"""Value objects, the buffer contract and the domain errors.

The cursor is a *value*, not a mutable object: every command stores the caret
it started from and the caret it ended at, which is what makes undo restore the
selection as well as the text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError


# --8<-- [start:enums]
class DocumentStatus(StrEnum):
    NEW = "new"  # never saved and never edited
    MODIFIED = "modified"  # the buffer differs from what storage holds
    SAVED = "saved"  # in sync with storage (undoing back here restores it)


class Direction(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    LINE_START = "line_start"
    LINE_END = "line_end"


# --8<-- [end:enums]


# --8<-- [start:errors]
class DocumentNotFoundError(NotFoundError):
    """No open tab with that name."""


class DocumentExistsError(ConflictError):
    """A tab with that name is already open."""


class OutOfBoundsError(ValidationError):
    """A position or range outside the document."""


class NothingToUndoError(InvalidStateError):
    """undo() with an empty undo stack, or redo() with an empty redo stack."""


class EmptyClipboardError(InvalidStateError):
    """paste() with nothing on the clipboard."""


# --8<-- [end:errors]


# --8<-- [start:cursor]
@dataclass(frozen=True, slots=True)
class Selection:
    """A normalised half-open range. ``start <= end`` always holds."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise OutOfBoundsError(f"invalid selection ({self.start}, {self.end})")

    @property
    def length(self) -> int:
        return self.end - self.start

    def is_empty(self) -> bool:
        return self.length == 0


@dataclass(frozen=True, slots=True)
class Cursor:
    """Caret position plus the anchor a shift-selection was started from."""

    position: int
    anchor: int | None = None

    def selection(self) -> Selection | None:
        if self.anchor is None or self.anchor == self.position:
            return None
        return Selection(min(self.anchor, self.position), max(self.anchor, self.position))

    def collapsed(self) -> Cursor:
        return Cursor(self.position)

    def moved_to(self, position: int, extend: bool = False) -> Cursor:
        anchor = (self.anchor if self.anchor is not None else self.position) if extend else None
        return Cursor(position, anchor)


# --8<-- [end:cursor]


# --8<-- [start:style]
@dataclass(frozen=True, slots=True)
class Style:
    """Flyweight: the intrinsic state of a run of text, shared by every run using it."""

    bold: bool = False
    italic: bool = False
    underline: bool = False
    colour: str = "default"

    def describe(self) -> str:
        flags = [name for name, on in (("b", self.bold), ("i", self.italic), ("u", self.underline)) if on]
        return f"{''.join(flags) or '-'}/{self.colour}"


@dataclass(frozen=True, slots=True)
class StyleRun:
    """The extrinsic state: where a shared Style applies."""

    start: int
    end: int
    style: Style


# --8<-- [end:style]


# --8<-- [start:protocols]
class TextBuffer(Protocol):
    """Strategy: the data structure under the text. Swap it without touching commands."""

    def __len__(self) -> int: ...
    def text(self) -> str: ...
    def slice(self, start: int, end: int) -> str: ...
    def insert(self, position: int, text: str) -> None: ...
    def delete(self, position: int, length: int) -> str: ...


class FileStorage(Protocol):
    """Where a document is persisted. In-memory in tests, a real path in production."""

    def save(self, name: str, content: str) -> None: ...
    def load(self, name: str) -> str: ...
    def exists(self, name: str) -> bool: ...


class DocumentListener(Protocol):
    """Observer: views that redraw when the document changes."""

    def on_document_changed(self, name: str, revision: int) -> None: ...


# --8<-- [end:protocols]
