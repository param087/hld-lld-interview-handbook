"""Two buffer strategies. The choice is the first thing an interviewer probes."""

from __future__ import annotations

from lld.text_editor.models import OutOfBoundsError


# --8<-- [start:simple]
class SimpleBuffer:
    """A plain string. Every insert copies the whole document: O(n) per keystroke.

    Correct, and fine up to a few hundred kilobytes. Name it as the baseline you
    would ship first, then say what you would replace it with and when.
    """

    def __init__(self, text: str = "") -> None:
        self._text = text

    def __len__(self) -> int:
        return len(self._text)

    def text(self) -> str:
        return self._text

    def slice(self, start: int, end: int) -> str:
        return self._text[max(0, start) : max(0, end)]

    def insert(self, position: int, text: str) -> None:
        self._check(position)
        self._text = self._text[:position] + text + self._text[position:]

    def delete(self, position: int, length: int) -> str:
        self._check(position + length)
        removed = self._text[position : position + length]
        self._text = self._text[:position] + self._text[position + length :]
        return removed

    def _check(self, position: int) -> None:
        if not 0 <= position <= len(self._text):
            raise OutOfBoundsError(f"position {position} outside 0..{len(self._text)}")


# --8<-- [end:simple]


# --8<-- [start:gap]
class GapBuffer:
    """A list of characters with a movable hole where the caret is.

    Typing at the caret writes into the hole: O(1) amortised, no copying. Moving
    the caret k characters copies k characters, which is exactly the workload a
    text editor has -- edits cluster where the caret already is. A piece table
    is the other classic answer: it never moves text and makes undo nearly free,
    at the cost of a rope-like index for random access. Say both, pick one, and
    give the reason: gap buffer for a single caret, piece table for collaborative
    editing where several carets touch the document at once.
    """

    MIN_GAP = 16

    def __init__(self, text: str = "", gap_size: int = MIN_GAP) -> None:
        self._chars: list[str] = list(text) + [""] * max(gap_size, 1)
        self._gap_start = len(text)
        self._gap_end = len(self._chars)

    def __len__(self) -> int:
        return len(self._chars) - (self._gap_end - self._gap_start)

    def text(self) -> str:
        return "".join(self._chars[: self._gap_start]) + "".join(self._chars[self._gap_end :])

    def slice(self, start: int, end: int) -> str:
        start, end = max(0, start), min(len(self), end)
        return "".join(self._chars[self._raw(i)] for i in range(start, end))

    def insert(self, position: int, text: str) -> None:
        self._check(position)
        self._move_gap(position)
        if self._gap_end - self._gap_start < len(text):
            self._grow(len(text))
        for char in text:
            self._chars[self._gap_start] = char
            self._gap_start += 1

    def delete(self, position: int, length: int) -> str:
        self._check(position + length)
        removed = self.slice(position, position + length)
        self._move_gap(position)
        self._gap_end += length  # swallow the following characters into the gap
        return removed

    def gap_size(self) -> int:
        return self._gap_end - self._gap_start

    def _raw(self, index: int) -> int:
        return index if index < self._gap_start else index + (self._gap_end - self._gap_start)

    def _check(self, position: int) -> None:
        if not 0 <= position <= len(self):
            raise OutOfBoundsError(f"position {position} outside 0..{len(self)}")

    def _move_gap(self, position: int) -> None:
        if position < self._gap_start:
            count = self._gap_start - position
            self._chars[self._gap_end - count : self._gap_end] = self._chars[position : self._gap_start]
            self._gap_start -= count
            self._gap_end -= count
        elif position > self._gap_start:
            count = position - self._gap_start
            self._chars[self._gap_start : self._gap_start + count] = self._chars[
                self._gap_end : self._gap_end + count
            ]
            self._gap_start += count
            self._gap_end += count

    def _grow(self, needed: int) -> None:
        extra = max(needed, len(self._chars) // 2, self.MIN_GAP)
        self._chars[self._gap_end : self._gap_end] = [""] * extra
        self._gap_end += extra


# --8<-- [end:gap]
