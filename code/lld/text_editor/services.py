"""The document (buffer + caret + history under one lock) and the editor facade."""

from __future__ import annotations

import threading
from collections.abc import Callable

from common import Clock, SystemClock
from lld.text_editor.buffers import GapBuffer
from lld.text_editor.commands import (
    Command,
    CommandHistory,
    DeleteCommand,
    InsertCommand,
    MacroCommand,
    ReplaceCommand,
)
from lld.text_editor.models import (
    Cursor,
    DocumentExistsError,
    DocumentListener,
    DocumentNotFoundError,
    DocumentStatus,
    FileStorage,
    OutOfBoundsError,
    Selection,
    Style,
    StyleRun,
    TextBuffer,
)
from lld.text_editor.support import Clipboard, InMemoryStorage, StyleRegistry


# --8<-- [start:document]
class Document:
    """One tab: a buffer, a caret, a style list and its own undo history.

    ``_lock`` is an ``RLock`` shared with the ``CommandHistory``. That sharing is
    the point: applying an edit and pushing it onto the undo stack must be one
    atomic step, or a second thread can undo a command whose text is not in the
    buffer yet. One reentrant lock also means there is only one lock order, so
    the design cannot deadlock against itself.
    """

    def __init__(
        self,
        name: str,
        buffer: TextBuffer | None = None,
        clock: Clock | None = None,
        history_capacity: int = 100,
        coalesce_window: float = 1.0,
    ) -> None:
        self.name = name
        self._lock = threading.RLock()
        self._buffer: TextBuffer = buffer if buffer is not None else GapBuffer()
        self._cursor = Cursor(len(self._buffer))
        self.history = CommandHistory(history_capacity, coalesce_window, clock or SystemClock(), self._lock)
        self._styles: list[StyleRun] = []
        self._revision = 0
        self._saved_depth: int | None = None
        self._listeners: list[DocumentListener] = []

    # --- EditTarget: the only three methods a Command may call -------------------
    def apply_insert(self, position: int, text: str) -> None:
        with self._lock:
            self._buffer.insert(position, text)
            self._shift_styles(position, len(text))
            self._revision += 1

    def apply_delete(self, position: int, length: int) -> str:
        with self._lock:
            removed = self._buffer.delete(position, length)
            self._shift_styles(position, -length)
            self._revision += 1
            return removed

    def set_cursor(self, cursor: Cursor) -> None:
        with self._lock:
            if not 0 <= cursor.position <= len(self._buffer):
                raise OutOfBoundsError(f"cursor {cursor.position} outside 0..{len(self._buffer)}")
            self._cursor = cursor

    # --- edits ------------------------------------------------------------------
    def run(self, command: Command) -> Command:
        """Execute and record atomically, then notify listeners outside the lock."""
        with self._lock:
            command.execute(self)
            self.history.record(command)
        self._notify()
        return command

    def undo(self) -> Command:
        with self._lock:
            command = self.history.undo(self)
        self._notify()
        return command

    def redo(self) -> Command:
        with self._lock:
            command = self.history.redo(self)
        self._notify()
        return command

    # --- reads ------------------------------------------------------------------
    def text(self) -> str:
        with self._lock:
            return self._buffer.text()

    def slice(self, start: int, end: int) -> str:
        with self._lock:
            return self._buffer.slice(start, end)

    def length(self) -> int:
        with self._lock:
            return len(self._buffer)

    def cursor(self) -> Cursor:
        with self._lock:
            return self._cursor

    def selection(self) -> Selection | None:
        with self._lock:
            return self._cursor.selection()

    def revision(self) -> int:
        with self._lock:
            return self._revision

    def word_count(self) -> int:
        return len(self.text().split())

    @property
    def status(self) -> DocumentStatus:
        """Undoing back to the depth at which you saved shows SAVED again."""
        with self._lock:
            if self._saved_depth is None:
                return DocumentStatus.NEW if self._revision == 0 else DocumentStatus.MODIFIED
            return DocumentStatus.SAVED if self._saved_depth == self.history.depth() else DocumentStatus.MODIFIED

    def mark_saved(self) -> None:
        with self._lock:
            self._saved_depth = self.history.depth()
            self.history.break_coalescing()

    # --- styles and observers ----------------------------------------------------
    def apply_style(self, start: int, end: int, style: Style) -> None:
        with self._lock:
            self._styles.append(StyleRun(start, end, style))

    def styles_at(self, position: int) -> list[Style]:
        with self._lock:
            return [run.style for run in self._styles if run.start <= position < run.end]

    def style_runs(self) -> list[StyleRun]:
        with self._lock:
            return list(self._styles)

    def subscribe(self, listener: DocumentListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def _shift_styles(self, position: int, delta: int) -> None:
        shifted: list[StyleRun] = []
        for run in self._styles:
            start = run.start + delta if run.start >= position else run.start
            end = run.end + delta if run.end > position else run.end
            if end > start:
                shifted.append(StyleRun(max(0, start), max(0, end), run.style))
        self._styles = shifted

    def _notify(self) -> None:
        with self._lock:
            listeners, name, revision = list(self._listeners), self.name, self._revision
        for listener in listeners:  # outside the lock: a slow view never blocks typing
            listener.on_document_changed(name, revision)


# --8<-- [end:document]


# --8<-- [start:editor]
class Editor:
    """The facade a keyboard talks to. Every mutation becomes exactly one Command."""

    def __init__(
        self,
        storage: FileStorage | None = None,
        clipboard: Clipboard | None = None,
        clock: Clock | None = None,
        history_capacity: int = 100,
        coalesce_window: float = 1.0,
        buffer_factory: Callable[[str], TextBuffer] | None = None,
    ) -> None:
        self.storage: FileStorage = storage or InMemoryStorage()
        self.clipboard = clipboard or Clipboard()
        self.styles = StyleRegistry()
        self._clock = clock or SystemClock()
        self._history_capacity = history_capacity
        self._coalesce_window = coalesce_window
        self._buffer_factory: Callable[[str], TextBuffer] = buffer_factory or GapBuffer
        self._lock = threading.Lock()  # guards the tab registry only
        self._tabs: dict[str, Document] = {}
        self._active: str | None = None

    # --- tabs --------------------------------------------------------------------
    def new_tab(self, name: str, text: str = "") -> Document:
        with self._lock:
            if name in self._tabs:
                raise DocumentExistsError(f"tab {name!r} is already open")
            document = Document(
                name, self._buffer_factory(text), self._clock, self._history_capacity, self._coalesce_window
            )
            self._tabs[name] = document
            self._active = name
            return document

    def open_tab(self, name: str) -> Document:
        """Load from storage into a new tab; the loaded text is not an undoable edit."""
        document = self.new_tab(name, self.storage.load(name))
        document.mark_saved()
        return document

    def close_tab(self, name: str) -> None:
        with self._lock:
            if name not in self._tabs:
                raise DocumentNotFoundError(f"no open tab named {name!r}")
            del self._tabs[name]
            self._active = next(iter(self._tabs), None)

    def switch_to(self, name: str) -> Document:
        with self._lock:
            if name not in self._tabs:
                raise DocumentNotFoundError(f"no open tab named {name!r}")
            self._active = name
            return self._tabs[name]

    def tabs(self) -> list[str]:
        with self._lock:
            return sorted(self._tabs)

    @property
    def active(self) -> Document:
        with self._lock:
            if self._active is None:
                raise DocumentNotFoundError("no open tabs")
            return self._tabs[self._active]

    # --- editing -----------------------------------------------------------------
    def type_text(self, text: str) -> Command:
        """Typing over a selection is a macro: delete the range, then insert."""
        document = self.active
        before = document.cursor()
        selection = before.selection()
        if selection is None:
            command: Command = InsertCommand(before.position, text, before, Cursor(before.position + len(text)))
        else:
            after = Cursor(selection.start + len(text))
            command = MacroCommand(
                "type over selection",
                before,
                after,
                [
                    DeleteCommand(selection.start, selection.length, before, Cursor(selection.start)),
                    InsertCommand(selection.start, text, Cursor(selection.start), after),
                ],
            )
        return document.run(command)

    def backspace(self) -> Command:
        document = self.active
        before = document.cursor()
        selection = before.selection()
        if selection is not None:
            return document.run(
                DeleteCommand(selection.start, selection.length, before, Cursor(selection.start))
            )
        if before.position == 0:
            raise OutOfBoundsError("nothing to delete before position 0")
        return document.run(DeleteCommand(before.position - 1, 1, before, Cursor(before.position - 1)))

    def delete_forward(self) -> Command:
        document = self.active
        before = document.cursor()
        if before.position >= document.length():
            raise OutOfBoundsError("nothing to delete after the end")
        return document.run(DeleteCommand(before.position, 1, before, before.collapsed()))

    def move_to(self, position: int, extend: bool = False) -> Cursor:
        """A caret move ends the current typing run, so undo stops at word boundaries."""
        document = self.active
        cursor = document.cursor().moved_to(position, extend)
        document.set_cursor(cursor)
        document.history.break_coalescing()
        return cursor

    def select(self, start: int, end: int) -> Selection:
        document = self.active
        document.set_cursor(Cursor(end, start))
        document.history.break_coalescing()
        return Selection(min(start, end), max(start, end))

    def select_all(self) -> Selection:
        return self.select(0, self.active.length())

    # --- clipboard ---------------------------------------------------------------
    def copy(self) -> str:
        document = self.active
        selection = document.selection()
        text = "" if selection is None else document.slice(selection.start, selection.end)
        self.clipboard.copy(text)
        return text

    def cut(self) -> str:
        text = self.copy()
        if text:
            self.backspace()
        return text

    def paste(self) -> Command:
        return self.type_text(self.clipboard.paste())

    # --- history -----------------------------------------------------------------
    def undo(self) -> Command:
        return self.active.undo()

    def redo(self) -> Command:
        return self.active.redo()

    # --- search, storage, stats ---------------------------------------------------
    def find(self, needle: str) -> list[int]:
        if not needle:
            raise OutOfBoundsError("cannot search for an empty string")
        text, hits, start = self.active.text(), [], 0
        while (index := text.find(needle, start)) != -1:
            hits.append(index)
            start = index + 1  # overlapping matches count
        return hits

    def replace_all(self, needle: str, replacement: str) -> Command | None:
        """One macro, one undo step -- and applied right to left so offsets stay valid."""
        hits = self.find(needle)
        if not hits:
            return None
        document = self.active
        before = document.cursor()
        after = Cursor(hits[0] + len(replacement))
        edits: list[Command] = [
            ReplaceCommand(index, len(needle), replacement, before, after) for index in reversed(hits)
        ]
        return document.run(MacroCommand(f"replace {needle!r}", before, after, edits))

    def save(self, name: str | None = None) -> str:
        document = self.active
        target = name or document.name
        self.storage.save(target, document.text())
        document.mark_saved()
        return target

    def word_count(self) -> int:
        return self.active.word_count()


# --8<-- [end:editor]
