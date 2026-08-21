"""Commands and the two stacks. This is the whole undo/redo design in one file."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from common import Clock, SystemClock
from lld.text_editor.models import Cursor, NothingToUndoError

MAX_COALESCED_RUN = 40  # stop merging keystrokes once one undo entry gets this long


# --8<-- [start:target]
class EditTarget(Protocol):
    """The three operations a command needs. ``Document`` implements it.

    Commands depend on this narrow protocol rather than on ``Document``, so the
    command layer has no idea about tabs, listeners, styles or storage.
    """

    def apply_insert(self, position: int, text: str) -> None: ...
    def apply_delete(self, position: int, length: int) -> str: ...
    def set_cursor(self, cursor: Cursor) -> None: ...


# --8<-- [end:target]


# --8<-- [start:commands]
class Command(ABC):
    """Every edit is an object that knows how to apply itself and how to take itself back.

    ``before`` and ``after`` are the caret states around the edit. Storing both
    is what makes undo restore the *selection* and not just the characters --
    the difference between an editor that feels right and one that does not.
    """

    before: Cursor
    after: Cursor

    @abstractmethod
    def execute(self, target: EditTarget) -> None: ...

    @abstractmethod
    def undo(self, target: EditTarget) -> None: ...

    @abstractmethod
    def describe(self) -> str: ...

    def coalesce_with(self, previous: Command) -> Command | None:
        """Return a merged command, or None when the two must stay separate."""
        return None


@dataclass(slots=True)
class InsertCommand(Command):
    position: int
    text: str
    before: Cursor
    after: Cursor

    def execute(self, target: EditTarget) -> None:
        target.apply_insert(self.position, self.text)
        target.set_cursor(self.after)

    def undo(self, target: EditTarget) -> None:
        target.apply_delete(self.position, len(self.text))
        target.set_cursor(self.before)

    def describe(self) -> str:
        return f"insert {self.text!r} at {self.position}"

    def coalesce_with(self, previous: Command) -> Command | None:
        """Merge only *typing*: adjacent, same direction, no newline, bounded length."""
        if not isinstance(previous, InsertCommand):
            return None
        if previous.position + len(previous.text) != self.position:
            return None
        if "\n" in previous.text or "\n" in self.text:
            return None
        if len(previous.text) + len(self.text) > MAX_COALESCED_RUN:
            return None
        return InsertCommand(previous.position, previous.text + self.text, previous.before, self.after)


@dataclass(slots=True)
class DeleteCommand(Command):
    position: int
    length: int
    before: Cursor
    after: Cursor
    removed: str = ""

    def execute(self, target: EditTarget) -> None:
        self.removed = target.apply_delete(self.position, self.length)
        target.set_cursor(self.after)

    def undo(self, target: EditTarget) -> None:
        target.apply_insert(self.position, self.removed)
        target.set_cursor(self.before)

    def describe(self) -> str:
        return f"delete {self.length} at {self.position}"

    def coalesce_with(self, previous: Command) -> Command | None:
        """Held-down backspace is one undo step, not thirty."""
        if not isinstance(previous, DeleteCommand) or self.length != 1 or previous.length > MAX_COALESCED_RUN:
            return None
        if self.position + self.length != previous.position:
            return None
        merged = DeleteCommand(
            self.position, self.length + previous.length, previous.before, self.after, self.removed + previous.removed
        )
        return merged


@dataclass(slots=True)
class ReplaceCommand(Command):
    position: int
    old_length: int
    new_text: str
    before: Cursor
    after: Cursor
    removed: str = ""

    def execute(self, target: EditTarget) -> None:
        self.removed = target.apply_delete(self.position, self.old_length)
        target.apply_insert(self.position, self.new_text)
        target.set_cursor(self.after)

    def undo(self, target: EditTarget) -> None:
        target.apply_delete(self.position, len(self.new_text))
        target.apply_insert(self.position, self.removed)
        target.set_cursor(self.before)

    def describe(self) -> str:
        return f"replace {self.old_length} at {self.position} with {self.new_text!r}"


@dataclass(slots=True)
class MacroCommand(Command):
    """Composite: many edits, one undo step.

    Paste, type-over-a-selection and replace-all are all macros. That is why
    there is no PasteCommand class -- paste is delete plus insert, and inventing
    a third class for it would be duplication dressed up as design.
    """

    label: str
    before: Cursor
    after: Cursor
    commands: list[Command] = field(default_factory=list)

    def execute(self, target: EditTarget) -> None:
        for command in self.commands:
            command.execute(target)
        target.set_cursor(self.after)

    def undo(self, target: EditTarget) -> None:
        for command in reversed(self.commands):
            command.undo(target)
        target.set_cursor(self.before)

    def describe(self) -> str:
        return f"{self.label} ({len(self.commands)} edits)"


# --8<-- [end:commands]


# --8<-- [start:history]
class CommandHistory:
    """Two stacks, a coalescing window and a cap.

    Three rules an interviewer checks for:

    1. a new command clears the redo stack -- the future you undid is gone;
    2. consecutive keystrokes merge into one entry while they stay adjacent and
       arrive inside ``coalesce_window`` seconds of each other;
    3. the undo stack is bounded, and the *oldest* entry is dropped, because the
       recent past is what a user actually undoes.
    """

    def __init__(
        self,
        capacity: int = 100,
        coalesce_window: float = 1.0,
        clock: Clock | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        self.capacity = capacity
        self.coalesce_window = coalesce_window
        self._clock = clock or SystemClock()
        # Shared with the document on purpose: the buffer and the stacks must
        # move together, so they are guarded by one reentrant lock.
        self._lock = lock or threading.RLock()
        self._undo: deque[Command] = deque()
        self._redo: list[Command] = []
        self._last_at: float | None = None
        self._can_coalesce = True
        self.dropped = 0

    def record(self, command: Command) -> None:
        with self._lock:
            self._redo.clear()  # rule 1
            now = self._clock.now()
            gap = None if self._last_at is None else now - self._last_at
            merged = None
            if self._can_coalesce and self._undo and gap is not None and gap <= self.coalesce_window:
                merged = command.coalesce_with(self._undo[-1])  # rule 2
            if merged is not None:
                self._undo[-1] = merged
            else:
                self._undo.append(command)
                while len(self._undo) > self.capacity:  # rule 3
                    self._undo.popleft()
                    self.dropped += 1
            self._last_at = now
            self._can_coalesce = True

    def undo(self, target: EditTarget) -> Command:
        with self._lock:
            if not self._undo:
                raise NothingToUndoError("nothing to undo")
            command = self._undo.pop()
            command.undo(target)
            self._redo.append(command)
            self._can_coalesce = False  # typing after an undo starts a fresh entry
            return command

    def redo(self, target: EditTarget) -> Command:
        with self._lock:
            if not self._redo:
                raise NothingToUndoError("nothing to redo")
            command = self._redo.pop()
            command.execute(target)
            self._undo.append(command)
            self._can_coalesce = False
            return command

    def break_coalescing(self) -> None:
        """Called on a cursor move or a save: the next keystroke starts a new entry."""
        with self._lock:
            self._can_coalesce = False

    def depth(self) -> int:
        """Total edits ever applied and still applied."""
        with self._lock:
            return len(self._undo) + self.dropped

    def top(self) -> Command | None:
        """The command an undo would take back -- the document's save marker.

        Identity, not a count: after saving, undoing once and typing something
        new, the stack is the same *depth* but a different *history*, and a
        counter would wrongly report the document as saved.
        """
        with self._lock:
            return self._undo[-1] if self._undo else None

    def can_undo(self) -> bool:
        with self._lock:
            return bool(self._undo)

    def can_redo(self) -> bool:
        with self._lock:
            return bool(self._redo)

    def labels(self) -> list[str]:
        with self._lock:
            return [command.describe() for command in self._undo]


# --8<-- [end:history]
