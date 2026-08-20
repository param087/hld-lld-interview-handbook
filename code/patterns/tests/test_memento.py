"""Memento: the caretaker stacks opaque snapshots, the originator is the only one that reads them."""

import dataclasses

import pytest

from common import InvalidStateError, ValidationError
from patterns.memento import EditorSnapshot, History, KeyValueStore, TextEditor


def typed(editor: TextEditor, history: History[EditorSnapshot], *fragments: str) -> None:
    for fragment in fragments:
        history.checkpoint()
        editor.insert(fragment)


def test_undo_and_redo_walk_the_snapshots_in_order() -> None:
    editor = TextEditor()
    history = History(editor)
    typed(editor, history, "Hello", ", world", "!")
    assert (editor.text, editor.cursor) == ("Hello, world!", 13)

    history.undo()
    assert (editor.text, editor.cursor) == ("Hello, world", 12)
    history.undo()
    assert (editor.text, editor.cursor) == ("Hello", 5)
    history.redo()
    assert (editor.text, editor.cursor) == ("Hello, world", 12)
    assert (history.undo_depth, history.redo_depth) == (2, 1)


def test_a_new_edit_after_undo_abandons_the_redo_branch() -> None:
    editor = TextEditor()
    history = History(editor)
    typed(editor, history, "Hello", " world")
    history.undo()
    assert history.redo_depth == 1

    typed(editor, history, " there")
    assert editor.text == "Hello there"
    assert history.redo_depth == 0
    with pytest.raises(InvalidStateError):
        history.redo()


def test_history_limit_drops_the_oldest_snapshot() -> None:
    editor = TextEditor()
    history = History(editor, limit=2)
    typed(editor, history, "a", "b", "c")
    assert history.undo_depth == 2  # the snapshot of the empty document is gone

    history.undo()
    history.undo()
    assert editor.text == "a"
    with pytest.raises(InvalidStateError):
        history.undo()


@pytest.mark.parametrize("limit", [0, -1])
def test_history_rejects_a_limit_without_room(limit: int) -> None:
    with pytest.raises(ValidationError):
        History(TextEditor(), limit=limit)


def test_snapshots_are_immutable_values_that_can_be_restored_more_than_once() -> None:
    editor = TextEditor("draft")
    snapshot = editor.save()
    assert snapshot == EditorSnapshot("draft", 5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.text = "hacked"  # type: ignore[misc]

    editor.insert("!")
    editor.restore(snapshot)
    editor.backspace(5)
    editor.restore(snapshot)  # a snapshot is not consumed by restoring it
    assert (editor.text, editor.cursor) == ("draft", 5)


def test_caretaker_is_generic_and_needs_no_base_class_from_the_originator() -> None:
    class Counter:
        def __init__(self) -> None:
            self.value = 0

        def save(self) -> int:  # the snapshot is a plain int; the caretaker never inspects it
            return self.value

        def restore(self, snapshot: int) -> None:
            self.value = snapshot

    counter = Counter()
    history = History(counter)
    for _ in range(3):
        history.checkpoint()
        counter.value += 10
    history.undo()
    history.undo()
    assert counter.value == 10
    history.redo()
    assert counter.value == 20


def test_nested_savepoints_roll_back_and_commit_like_sql() -> None:
    store = KeyValueStore()
    store.set("a", "1")
    store.begin()
    store.set("a", "2")
    store.set("b", "x")
    store.begin()
    store.delete("a")
    assert (store.get("a"), store.depth) == (None, 2)

    store.rollback()
    assert (store.get("a"), store.get("b"), store.depth) == ("2", "x", 1)
    store.commit()
    assert (store.get("a"), store.get("b"), store.depth) == ("2", "x", 0)

    store.begin()
    store.set("a", "3")
    store.rollback()
    assert store.get("a") == "2"


def test_rollback_or_commit_without_a_transaction_is_rejected() -> None:
    store = KeyValueStore()
    with pytest.raises(InvalidStateError):
        store.rollback()
    with pytest.raises(InvalidStateError):
        store.commit()


@pytest.mark.parametrize("position", [-1, 6])
def test_editor_validates_cursor_moves(position: int) -> None:
    with pytest.raises(ValidationError):
        TextEditor("draft").move_cursor(position)
