"""Command: edits are objects, so the history can undo, redo, merge and log them without reading one."""

from collections import deque
from functools import partial

import pytest

from common import InvalidStateError, ValidationError
from patterns.command import (
    Command,
    CommandHistory,
    DeleteText,
    Document,
    InsertText,
    MacroCommand,
    UndoStack,
    delete_step,
    insert_step,
    run_queued,
)


def test_execute_undo_and_redo_walk_the_same_commands_back_and_forth() -> None:
    document = Document("Hello")
    history = CommandHistory()
    history.execute(InsertText(document, 5, ", world"))
    history.execute(DeleteText(document, 0, 7))
    assert document.text == "world"
    history.undo()
    assert document.text == "Hello, world"
    history.undo()
    assert document.text == "Hello"
    assert (history.undo_depth, history.redo_depth) == (0, 2)
    history.redo()
    history.redo()
    assert document.text == "world"
    assert history.steps() == ["insert ', world' at 5", "delete 7 chars at 0"]


def test_a_new_command_abandons_the_redo_branch() -> None:
    document = Document()
    history = CommandHistory()
    history.execute(InsertText(document, 0, "abc"))
    history.undo()
    assert history.redo_depth == 1
    history.execute(InsertText(document, 0, "xyz"))
    assert history.redo_depth == 0
    with pytest.raises(InvalidStateError):
        history.redo()
    assert document.text == "xyz"


def test_consecutive_typing_coalesces_but_not_across_a_gap_or_after_undo() -> None:
    document = Document()
    history = CommandHistory()
    history.execute(InsertText(document, 0, "Hel"))
    history.execute(InsertText(document, 3, "lo"))
    assert history.undo_depth == 1
    assert history.steps() == ["insert 'Hello' at 0"]
    history.execute(InsertText(document, 0, "> "))  # not adjacent to the previous insert
    assert history.undo_depth == 2
    history.undo()
    history.redo()
    history.execute(InsertText(document, 2, "!"))  # adjacent, but the previous step came from redo
    assert history.undo_depth == 3
    history.undo()
    assert document.text == "> Hello"
    history.undo()
    history.undo()
    assert document.text == ""
    other = Document()
    assert InsertText(document, 0, "a").coalesce(InsertText(other, 1, "b")) is None


def test_a_macro_is_one_undo_step_and_rolls_back_when_a_part_fails() -> None:
    document = Document("Hello, world")
    history = CommandHistory()
    replace = MacroCommand("replace", [DeleteText(document, 7, 5), InsertText(document, 7, "there")])
    history.execute(replace)
    assert document.text == "Hello, there"
    assert history.undo_depth == 1
    assert history.steps() == ["replace: delete 5 chars at 7, insert 'there' at 7"]
    history.undo()
    assert document.text == "Hello, world"

    broken = MacroCommand("broken", [DeleteText(document, 0, 7), InsertText(document, 99, "x")])
    with pytest.raises(ValidationError):
        history.execute(broken)
    assert document.text == "Hello, world"  # the delete that ran was undone
    assert (history.undo_depth, history.redo_depth) == (0, 1)  # the redo branch survived too
    with pytest.raises(ValidationError):
        MacroCommand("empty", [])


def test_only_commands_that_ran_are_recorded() -> None:
    document = Document("abc")
    history = CommandHistory()
    with pytest.raises(ValidationError):
        history.execute(InsertText(document, 10, "x"))
    with pytest.raises(ValidationError):
        history.execute(DeleteText(document, 2, 5))
    assert document.text == "abc"
    assert history.undo_depth == 0
    with pytest.raises(InvalidStateError):
        history.undo()


def test_a_delete_cannot_be_undone_before_it_ran_or_twice() -> None:
    document = Document("abc")
    delete = DeleteText(document, 0, 1)
    with pytest.raises(InvalidStateError):
        delete.undo()
    delete.execute()
    delete.undo()
    assert document.text == "abc"
    with pytest.raises(InvalidStateError):
        delete.undo()


def test_the_history_is_capped_and_drops_the_oldest_step() -> None:
    document = Document()
    history = CommandHistory(limit=2)
    for position, fragment in ((0, "a"), (0, "b"), (0, "c")):  # prepends never coalesce
        history.execute(InsertText(document, position, fragment))
    assert document.text == "cba"
    assert history.undo_depth == 2
    history.undo()
    history.undo()
    assert document.text == "a"
    with pytest.raises(InvalidStateError):
        history.undo()
    with pytest.raises(ValidationError):
        CommandHistory(limit=0)


def test_the_invoker_needs_only_execute_and_undo_from_a_command() -> None:
    class Spy(Command):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def execute(self) -> None:
            self.calls.append("execute")

        def undo(self) -> None:
            self.calls.append("undo")

    spy = Spy()
    history = CommandHistory()
    history.execute(spy)
    history.undo()
    history.redo()
    assert spy.calls == ["execute", "undo", "execute"]
    assert history.steps() == ["Spy"]
    assert spy.coalesce(Spy()) is None


@pytest.mark.parametrize(
    ("text", "position", "fragment"),
    [("", 0, "Hello"), ("Hello", 5, ", world"), ("Hello, world", 0, "> ")],
)
def test_partials_agree_with_the_class_form(text: str, position: int, fragment: str) -> None:
    by_class, by_partial = Document(text), Document(text)
    InsertText(by_class, position, fragment).execute()
    stack = UndoStack()
    stack.run(*insert_step(by_partial, position, fragment))
    assert by_partial.text == by_class.text
    stack.undo()
    assert by_partial.text == text
    stack.redo()
    assert by_partial.text == by_class.text


def test_a_closure_keeps_the_removed_text_and_the_stack_walks_both_ways() -> None:
    document = Document("Hello, world")
    stack = UndoStack()
    stack.run(*delete_step(document, 0, 7))
    stack.run(*insert_step(document, 5, "!"))
    assert document.text == "world!"
    stack.undo()
    stack.undo()
    assert document.text == "Hello, world"
    with pytest.raises(InvalidStateError):
        stack.undo()
    stack.redo()
    assert document.text == "world"
    stack.run(*insert_step(document, 0, "the "))  # abandons the remaining redo step
    with pytest.raises(InvalidStateError):
        stack.redo()


def test_a_queue_of_partials_runs_later_without_the_worker_knowing_the_receiver() -> None:
    document = Document("abc")
    queue = deque([partial(document.insert, 0, "x"), partial(document.delete, 1, 3)])
    assert document.text == "abc"  # nothing has run yet
    assert run_queued(queue) == 2
    assert document.text == "x"
    assert run_queued(queue) == 0
