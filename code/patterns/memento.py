"""Memento: snapshot an object's state and restore it later without exposing its internals.

The running example is a text editor with undo and redo. ``TextEditor`` (the
Originator) is the only class that knows what its state looks like; it hands out
``EditorSnapshot`` values (the Memento) and takes them back in ``restore``.
``History`` (the Caretaker) stacks snapshots and decides *when* to restore, but it
is generic over the snapshot type and therefore cannot read a single field of one.
The last section shows the same idea as database-style savepoints over a plain
``dict``, where the memento is nothing more than a copy.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol

from common import InvalidStateError, ValidationError

DEFAULT_HISTORY_LIMIT = 100


# --8<-- [start:originator]
class Originator[S](Protocol):
    """Anything that can export its state as an opaque snapshot and import it again.

    ``S`` is whatever the originator chooses; the caretaker only ever passes it back.
    """

    def save(self) -> S: ...

    def restore(self, snapshot: S) -> None: ...


@dataclass(frozen=True, slots=True)
class EditorSnapshot:
    """The Memento: an immutable copy of the editor's state at one instant.

    ``str`` is immutable, so the snapshot shares the document with the editor
    instead of copying it; a mutable field (a list of lines) would need a copy here.
    """

    text: str
    cursor: int


class TextEditor:
    """The Originator: owns the state, produces snapshots and applies them.

    Nothing outside the class can write ``_text`` or ``_cursor``; the only way to
    move the editor backwards in time is through ``restore``.
    """

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._cursor = len(text)

    @property
    def text(self) -> str:
        return self._text

    @property
    def cursor(self) -> int:
        return self._cursor

    def insert(self, fragment: str) -> None:
        self._text = self._text[: self._cursor] + fragment + self._text[self._cursor :]
        self._cursor += len(fragment)

    def backspace(self, count: int = 1) -> None:
        if count < 0:
            raise ValidationError("cannot delete a negative number of characters")
        count = min(count, self._cursor)
        self._text = self._text[: self._cursor - count] + self._text[self._cursor :]
        self._cursor -= count

    def move_cursor(self, position: int) -> None:
        if not 0 <= position <= len(self._text):
            raise ValidationError(f"cursor {position} is outside the document")
        self._cursor = position

    def save(self) -> EditorSnapshot:
        return EditorSnapshot(self._text, self._cursor)

    def restore(self, snapshot: EditorSnapshot) -> None:
        self._text = snapshot.text
        self._cursor = snapshot.cursor


# --8<-- [end:originator]


# --8<-- [start:caretaker]
class History[S]:
    """The Caretaker: keeps snapshots in order and never looks inside one.

    ``checkpoint`` is called *before* a mutation; ``undo`` and ``redo`` move the
    originator between snapshots. The undo stack is bounded: the oldest snapshot
    is dropped when the limit is reached, so memory stays at limit x snapshot size.
    """

    def __init__(self, originator: Originator[S], limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        if limit < 1:
            raise ValidationError("history needs room for at least one snapshot")
        self._originator = originator
        self._undo: deque[S] = deque(maxlen=limit)
        self._redo: list[S] = []

    def checkpoint(self) -> None:
        """Record the current state; any redo branch is abandoned."""
        self._undo.append(self._originator.save())
        self._redo.clear()

    def undo(self) -> None:
        if not self._undo:
            raise InvalidStateError("nothing to undo")
        self._redo.append(self._originator.save())
        self._originator.restore(self._undo.pop())

    def redo(self) -> None:
        if not self._redo:
            raise InvalidStateError("nothing to redo")
        self._undo.append(self._originator.save())
        self._originator.restore(self._redo.pop())

    @property
    def undo_depth(self) -> int:
        return len(self._undo)

    @property
    def redo_depth(self) -> int:
        return len(self._redo)


# --8<-- [end:caretaker]


# --8<-- [start:savepoints]
class KeyValueStore:
    """Savepoints over a dict: the memento is a plain copy, the caretaker is a list.

    ``begin`` snapshots, ``rollback`` restores the newest snapshot and ``commit``
    discards it, which is how SQL ``SAVEPOINT`` nests. Values are immutable
    strings, so a shallow copy is a complete snapshot.
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._savepoints: list[dict[str, str]] = []

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def begin(self) -> None:
        self._savepoints.append(dict(self._data))

    def rollback(self) -> None:
        if not self._savepoints:
            raise InvalidStateError("no transaction to roll back")
        self._data = self._savepoints.pop()

    def commit(self) -> None:
        if not self._savepoints:
            raise InvalidStateError("no transaction to commit")
        self._savepoints.pop()

    @property
    def depth(self) -> int:
        return len(self._savepoints)


# --8<-- [end:savepoints]


def main() -> None:
    editor = TextEditor()
    history = History(editor, limit=3)
    print("--- typing with a history capped at 3 snapshots ---")
    for fragment in ("Hello", ",", " world", "!"):
        history.checkpoint()
        editor.insert(fragment)
        print(f"typed {fragment!r:>8} -> {editor.text!r:<16} undo depth {history.undo_depth}")
    history.undo()
    print(f"undo            -> {editor.text!r:<16} redo depth {history.redo_depth}")
    history.undo()
    print(f"undo            -> {editor.text!r:<16} redo depth {history.redo_depth}")
    history.redo()
    print(f"redo            -> {editor.text!r:<16} redo depth {history.redo_depth}")
    history.checkpoint()
    editor.backspace(6)
    editor.insert(" there")
    print(f"new edit        -> {editor.text!r:<16} redo depth {history.redo_depth} (branch abandoned)")
    for _ in range(3):
        history.undo()
    print(f"undo x3         -> {editor.text!r:<16} undo depth {history.undo_depth} (oldest was dropped)")
    try:
        history.undo()
    except InvalidStateError as exc:
        print(f"rejected: {exc}")

    print("--- savepoints over a dict: the memento is a copy ---")
    store = KeyValueStore()
    store.set("a", "1")
    store.begin()
    store.set("a", "2")
    store.begin()
    store.delete("a")
    print(f"inner transaction: a={store.get('a')} depth {store.depth}")
    store.rollback()
    print(f"rollback inner:    a={store.get('a')} depth {store.depth}")
    store.commit()
    print(f"commit outer:      a={store.get('a')} depth {store.depth}")


if __name__ == "__main__":
    main()
