"""Command: a request as an object, so it can be undone, redone, queued and logged.

The running example is a text editor. ``Document`` (the Receiver) is the only
class that knows how to change the text; ``InsertText`` and ``DeleteText`` (the
Commands) each carry the arguments of one edit and know how to reverse it;
``MacroCommand`` groups several edits into one step; ``CommandHistory`` (the
Invoker) executes commands and keeps the undo and redo stacks without knowing
what any command does. The second half restates the idea as plain callables:
``functools.partial`` binds the receiver and the arguments now so an invoker can
call later, and ``(do, undo)`` pairs give undo without a class per edit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterable
from functools import partial

from common import InvalidStateError, ValidationError

DEFAULT_HISTORY_LIMIT = 100


# --8<-- [start:receiver]
class Document:
    """The Receiver: owns the text and knows how to change it. It knows nothing about undo."""

    def __init__(self, text: str = "") -> None:
        self._text = text

    @property
    def text(self) -> str:
        return self._text

    def insert(self, position: int, fragment: str) -> None:
        if not 0 <= position <= len(self._text):
            raise ValidationError(f"position {position} is outside the document")
        self._text = self._text[:position] + fragment + self._text[position:]

    def delete(self, position: int, length: int) -> str:
        """Remove ``length`` characters at ``position`` and return them, so a caller can put them back."""
        if length < 0 or not 0 <= position <= len(self._text) - length:
            raise ValidationError(f"cannot delete {length} characters at {position}")
        removed = self._text[position : position + length]
        self._text = self._text[:position] + self._text[position + length :]
        return removed


# --8<-- [end:receiver]


# --8<-- [start:commands]
class Command(ABC):
    """The Command interface: do it, undo it, and say what it was.

    An ``ABC`` rather than a ``Protocol`` because two methods have shared defaults:
    ``coalesce`` (most commands never merge) and ``describe`` (for the log). A
    command is bound to its receiver and its arguments at construction, so whoever
    executes it later needs neither.
    """

    @abstractmethod
    def execute(self) -> None: ...

    @abstractmethod
    def undo(self) -> None:
        """Reverse ``execute``. Only valid after ``execute`` has run."""

    def coalesce(self, following: Command) -> Command | None:
        """One command equivalent to ``self`` then ``following``, or ``None`` to keep both."""
        return None

    def describe(self) -> str:
        return type(self).__name__


class InsertText(Command):
    """Insert a fragment at a position; undo deletes exactly that many characters there."""

    def __init__(self, document: Document, position: int, fragment: str) -> None:
        self._document = document
        self.position = position
        self.fragment = fragment

    def execute(self) -> None:
        self._document.insert(self.position, self.fragment)

    def undo(self) -> None:
        self._document.delete(self.position, len(self.fragment))

    def coalesce(self, following: Command) -> Command | None:
        """Consecutive typing is one undo step: 'Hel' then 'lo' at the next position is 'Hello'."""
        if (
            isinstance(following, InsertText)
            and following._document is self._document
            and following.position == self.position + len(self.fragment)
        ):
            return InsertText(self._document, self.position, self.fragment + following.fragment)
        return None

    def describe(self) -> str:
        return f"insert {self.fragment!r} at {self.position}"


class DeleteText(Command):
    """Delete a range; the removed text is captured during ``execute`` so ``undo`` can restore it.

    The command carries a small memento of its own: what it needs to reverse itself
    and nothing more. Before ``execute`` there is nothing to restore, hence the guard.
    """

    def __init__(self, document: Document, position: int, length: int) -> None:
        self._document = document
        self.position = position
        self.length = length
        self._removed: str | None = None

    def execute(self) -> None:
        self._removed = self._document.delete(self.position, self.length)

    def undo(self) -> None:
        if self._removed is None:
            raise InvalidStateError("cannot undo a delete that has not run")
        self._document.insert(self.position, self._removed)
        self._removed = None

    def describe(self) -> str:
        return f"delete {self.length} chars at {self.position}"


class MacroCommand(Command):
    """Several commands as one: a replace is a delete then an insert, undone together, in reverse.

    All or nothing: if a part fails halfway, the parts that ran are undone and the
    error propagates, so the history never records a half-applied step.
    """

    def __init__(self, name: str, commands: Iterable[Command]) -> None:
        self._name = name
        self._commands = tuple(commands)
        if not self._commands:
            raise ValidationError("a macro needs at least one command")

    def execute(self) -> None:
        done: list[Command] = []
        try:
            for command in self._commands:
                command.execute()
                done.append(command)
        except Exception:
            for command in reversed(done):
                command.undo()
            raise

    def undo(self) -> None:
        for command in reversed(self._commands):
            command.undo()

    def describe(self) -> str:
        return f"{self._name}: " + ", ".join(command.describe() for command in self._commands)


# --8<-- [end:commands]


# --8<-- [start:invoker]
class CommandHistory:
    """The Invoker: runs commands and keeps the undo and redo stacks. It never reads a command.

    A command is recorded only after it succeeded, so the stacks hold nothing that
    did not happen. A new command abandons the redo branch. Consecutive commands
    that can ``coalesce`` share one undo step, but only when the previous one was
    executed directly (``_last``), never when it was reached by undo or redo. The
    undo stack is bounded, so a long session costs at most ``limit`` commands.
    One history serves one document on one thread; cross-thread work goes through
    a queue of commands instead (see ``run_queued``).
    """

    def __init__(self, limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        if limit < 1:
            raise ValidationError("history needs room for at least one command")
        self._undo: deque[Command] = deque(maxlen=limit)
        self._redo: list[Command] = []
        self._last: Command | None = None

    def execute(self, command: Command) -> None:
        command.execute()
        self._redo.clear()
        merged = None
        if self._undo and self._undo[-1] is self._last:
            merged = self._undo[-1].coalesce(command)
        if merged is None:
            self._undo.append(command)
        else:
            self._undo[-1] = merged
        self._last = self._undo[-1]

    def undo(self) -> None:
        if not self._undo:
            raise InvalidStateError("nothing to undo")
        command = self._undo.pop()
        command.undo()
        self._redo.append(command)
        self._last = None

    def redo(self) -> None:
        if not self._redo:
            raise InvalidStateError("nothing to redo")
        command = self._redo.pop()
        command.execute()
        self._undo.append(command)
        self._last = None

    @property
    def undo_depth(self) -> int:
        return len(self._undo)

    @property
    def redo_depth(self) -> int:
        return len(self._redo)

    def steps(self) -> list[str]:
        """The undo steps as text, oldest first: commands are data, so the history can be shown."""
        return [command.describe() for command in self._undo]


# --8<-- [end:invoker]


# --8<-- [start:functional]
# A command with one method is a callable. ``partial`` binds the receiver and the
# arguments now; whoever holds the result calls it later, knowing neither.
type Action = Callable[[], None]


class UndoStack:
    """Undo and redo over ``(do, undo)`` pairs: the invoker when commands are plain callables."""

    def __init__(self) -> None:
        self._undo: list[tuple[Action, Action]] = []
        self._redo: list[tuple[Action, Action]] = []

    def run(self, do: Action, undo: Action) -> None:
        do()
        self._undo.append((do, undo))
        self._redo.clear()

    def undo(self) -> None:
        if not self._undo:
            raise InvalidStateError("nothing to undo")
        do, undo = self._undo.pop()
        undo()
        self._redo.append((do, undo))

    def redo(self) -> None:
        if not self._redo:
            raise InvalidStateError("nothing to redo")
        do, undo = self._redo.pop()
        do()
        self._undo.append((do, undo))


def insert_step(document: Document, position: int, fragment: str) -> tuple[Action, Action]:
    """The (do, undo) pair for an insert: two partials over the same receiver."""
    return partial(document.insert, position, fragment), partial(document.delete, position, len(fragment))


def delete_step(document: Document, position: int, length: int) -> tuple[Action, Action]:
    """The (do, undo) pair for a delete: the closure keeps the removed text, as ``DeleteText`` kept a field."""
    removed: list[str] = []

    def do() -> None:
        removed.append(document.delete(position, length))

    def undo() -> None:
        document.insert(position, removed.pop())

    return do, undo


def run_queued(queue: deque[Action]) -> int:
    """Queueing: a worker drains commands it never created. Returns how many it ran."""
    ran = 0
    while queue:
        queue.popleft()()
        ran += 1
    return ran


# --8<-- [end:functional]


def main() -> None:
    document = Document()
    history = CommandHistory()
    print("--- typing, undo, redo; consecutive inserts coalesce into one step ---")
    history.execute(InsertText(document, 0, "Hello"))
    print(f"insert 'Hello'   -> {document.text!r:<16} undo depth {history.undo_depth}")
    history.execute(InsertText(document, 5, ", world"))
    print(f"insert ', world' -> {document.text!r:<16} undo depth {history.undo_depth} (coalesced)")
    replace = MacroCommand("replace", [DeleteText(document, 7, 5), InsertText(document, 7, "there")])
    history.execute(replace)
    print(f"replace macro    -> {document.text!r:<16} undo depth {history.undo_depth}")
    history.undo()
    print(f"undo             -> {document.text!r:<16} redo depth {history.redo_depth}")
    history.undo()
    print(f"undo             -> {document.text!r:<16} redo depth {history.redo_depth}")
    history.redo()
    print(f"redo             -> {document.text!r:<16} redo depth {history.redo_depth}")
    history.execute(InsertText(document, 12, "!"))
    print(f"insert '!'       -> {document.text!r:<16} redo depth {history.redo_depth} (branch abandoned)")
    print("undo steps, oldest first:")
    for step in history.steps():
        print(f"  {step}")
    try:
        history.redo()
    except InvalidStateError as exc:
        print(f"rejected: {exc}")

    print("--- a macro that fails halfway leaves no trace ---")
    broken = MacroCommand("bad replace", [DeleteText(document, 0, 5), InsertText(document, 99, "x")])
    try:
        history.execute(broken)
    except ValidationError as exc:
        print(f"rejected: {exc}")
        print(f"text {document.text!r}, undo depth {history.undo_depth}")

    print("--- functional variant: partials and (do, undo) pairs ---")
    scratch = Document("abc")
    stack = UndoStack()
    stack.run(*insert_step(scratch, 3, "def"))
    stack.run(*delete_step(scratch, 0, 2))
    print(f"after two steps: {scratch.text!r}")
    stack.undo()
    stack.undo()
    print(f"after two undos: {scratch.text!r}")
    queue: deque[Action] = deque([partial(scratch.insert, 0, "x"), partial(scratch.delete, 1, 3)])
    print(f"worker ran {run_queued(queue)} queued commands -> {scratch.text!r}")


if __name__ == "__main__":
    main()
