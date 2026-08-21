from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock
from lld.text_editor.buffers import GapBuffer, SimpleBuffer
from lld.text_editor.commands import InsertCommand, MacroCommand
from lld.text_editor.models import (
    Cursor,
    DocumentExistsError,
    DocumentNotFoundError,
    DocumentStatus,
    EmptyClipboardError,
    NothingToUndoError,
    OutOfBoundsError,
)
from lld.text_editor.services import Document, Editor
from lld.text_editor.support import InMemoryStorage, StatusBar


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_700_000_000)


@pytest.fixture
def editor(clock: FakeClock) -> Editor:
    instance = Editor(storage=InMemoryStorage(), clock=clock, history_capacity=50, coalesce_window=1.0)
    instance.new_tab("notes.txt")
    return instance


def type_all(editor: Editor, clock: FakeClock, text: str, gap: float = 0.1) -> None:
    for char in text:
        editor.type_text(char)
        clock.advance(gap)


# --8<-- [start:coalescing]
def test_consecutive_typing_coalesces_but_a_pause_or_a_move_does_not(editor: Editor, clock: FakeClock) -> None:
    type_all(editor, clock, "hello")  # five keystrokes, 0.1 s apart
    document = editor.active
    assert document.text() == "hello" and len(document.history.labels()) == 1

    clock.advance(3)  # longer than the coalescing window
    editor.type_text("!")
    assert len(document.history.labels()) == 2

    editor.move_to(0)  # a caret move always ends the run
    editor.type_text("A")
    assert len(document.history.labels()) == 3
    assert document.text() == "Ahello!"

    editor.undo()
    assert document.text() == "hello!"  # one keystroke, one undo
    editor.undo()
    assert document.text() == "hello"  # the pause split the run here
    editor.undo()
    assert document.text() == ""  # all five coalesced keystrokes at once


# --8<-- [end:coalescing]


# --8<-- [start:redo]
def test_a_new_command_invalidates_the_redo_stack_and_undo_restores_the_selection(
    editor: Editor, clock: FakeClock
) -> None:
    document = editor.active
    type_all(editor, clock, "hello")
    clock.advance(3)
    editor.select(1, 4)  # anchor 1, caret 4 -> "ell" selected
    editor.type_text("EY")  # typing over a selection is one macro

    assert document.text() == "hEYo" and isinstance(document.history.labels(), list)
    editor.undo()
    assert document.text() == "hello"
    assert document.cursor() == Cursor(4, 1)  # the selection came back, not just the text
    assert document.history.can_redo() is True

    editor.type_text("?")  # a new command after an undo
    assert document.history.can_redo() is False
    with pytest.raises(NothingToUndoError):
        editor.redo()


# --8<-- [end:redo]


@pytest.mark.parametrize(
    ("action", "error"),
    [
        (lambda e: e.new_tab("notes.txt"), DocumentExistsError),
        (lambda e: e.switch_to("ghost.txt"), DocumentNotFoundError),
        (lambda e: e.move_to(99), OutOfBoundsError),
        (lambda e: e.find(""), OutOfBoundsError),
        (lambda e: e.backspace(), OutOfBoundsError),
        (lambda e: e.paste(), EmptyClipboardError),
    ],
)
def test_invalid_operations_are_rejected(editor: Editor, action, error) -> None:
    with pytest.raises(error):
        action(editor)


def test_document_status_walks_new_modified_saved_and_back(editor: Editor, clock: FakeClock) -> None:
    document = editor.active
    assert document.status is DocumentStatus.NEW

    type_all(editor, clock, "draft")
    assert document.status is DocumentStatus.MODIFIED

    editor.save()
    assert document.status is DocumentStatus.SAVED
    assert editor.storage.load("notes.txt") == "draft"

    clock.advance(3)
    editor.type_text("!")
    assert document.status is DocumentStatus.MODIFIED
    editor.undo()
    assert document.status is DocumentStatus.SAVED  # undone back to the saved point


def test_a_different_edit_at_the_same_depth_is_not_saved(editor: Editor, clock: FakeClock) -> None:
    """Save, undo, then type: same stack depth, different history, unsaved work."""
    document = editor.active
    type_all(editor, clock, "hello")
    clock.advance(3)
    type_all(editor, clock, " world")
    editor.save()
    assert document.status is DocumentStatus.SAVED

    editor.undo()
    clock.advance(3)
    editor.type_text("!!!")

    assert document.text() == "hello!!!"
    assert editor.storage.load("notes.txt") == "hello world"
    assert document.status is DocumentStatus.MODIFIED  # a depth counter would say SAVED here


def test_history_cap_drops_the_oldest_entries(clock: FakeClock) -> None:
    editor = Editor(clock=clock, history_capacity=3, coalesce_window=0.0)
    editor.new_tab("capped.txt")
    document = editor.active
    for char in "abcdef":
        editor.type_text(char)
        clock.advance(2)

    assert len(document.history.labels()) == 3 and document.history.dropped == 3
    while document.history.can_undo():
        editor.undo()
    assert document.text() == "abc"  # the first three edits are no longer reachable


# --8<-- [start:concurrency]
def test_concurrent_typing_loses_no_character(clock: FakeClock) -> None:
    editor = Editor(clock=clock, history_capacity=1000, coalesce_window=1.0)
    document = editor.new_tab("shared.txt")

    def hammer(worker: int) -> None:
        for _ in range(50):
            editor.type_text(chr(ord("a") + worker))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(hammer, range(8)))

    text = document.text()
    assert len(text) == 400  # buffer and history move under one lock: no lost update
    for worker in range(8):
        assert text.count(chr(ord("a") + worker)) == 50
    labels = document.history.labels()
    assert 0 < len(labels) <= 400  # adjacent keystrokes coalesced, however they interleaved
    assert all(len(label) <= len("insert '' at 400") + 40 for label in labels)  # runs stay bounded

    while document.history.can_undo():
        editor.undo()
    assert document.text() == ""  # every command undid exactly what it did


# --8<-- [end:concurrency]


def test_cut_and_paste_move_text_and_undo_as_single_steps(editor: Editor, clock: FakeClock) -> None:
    type_all(editor, clock, "hello world")
    clock.advance(3)
    editor.select(0, 6)
    assert editor.cut() == "hello " and editor.active.text() == "world"

    editor.move_to(editor.active.length())
    editor.paste()
    assert editor.active.text() == "worldhello "

    editor.undo()
    assert editor.active.text() == "world"
    editor.undo()
    assert editor.active.text() == "hello world"


def test_replace_all_is_one_undo_step_and_finds_overlaps(editor: Editor, clock: FakeClock) -> None:
    type_all(editor, clock, "aaa bab")
    clock.advance(3)
    assert editor.find("aa") == [0, 1]  # overlapping matches both count

    command = editor.replace_all("a", "X")
    assert isinstance(command, MacroCommand)
    assert editor.active.text() == "XXX bXb"
    editor.undo()
    assert editor.active.text() == "aaa bab"
    assert editor.replace_all("zzz", "q") is None


def test_replace_all_with_a_shorter_replacement_still_undoes(clock: FakeClock) -> None:
    """Mid-macro the document is shorter than the caret it started from."""
    editor = Editor(storage=InMemoryStorage(), clock=clock)
    document = editor.new_tab("shrink.txt", "the kitten sat on the kitten mat")

    editor.replace_all("kitten", "cat")
    assert document.text() == "the cat sat on the cat mat"
    editor.undo()
    assert document.text() == "the kitten sat on the kitten mat"
    editor.redo()
    assert document.text() == "the cat sat on the cat mat"


def test_replace_all_skips_overlapping_matches(clock: FakeClock) -> None:
    """`find` counts overlaps; replacing them all would corrupt each other."""
    editor = Editor(storage=InMemoryStorage(), clock=clock)
    document = editor.new_tab("overlap.txt", "aaaa")
    assert editor.find("aa") == [0, 1, 2]

    editor.replace_all("aa", "X")
    assert document.text() == "aaaa".replace("aa", "X") == "XX"
    editor.undo()
    assert document.text() == "aaaa"


def test_tabs_have_independent_documents_and_histories(editor: Editor, clock: FakeClock) -> None:
    type_all(editor, clock, "first")
    editor.new_tab("second.txt")
    type_all(editor, clock, "second")

    assert editor.tabs() == ["notes.txt", "second.txt"]
    editor.undo()
    assert editor.active.text() == "" and editor.switch_to("notes.txt").text() == "first"

    editor.close_tab("second.txt")
    assert editor.tabs() == ["notes.txt"]


def test_styles_are_shared_flyweights_and_shift_with_edits(editor: Editor, clock: FakeClock) -> None:
    type_all(editor, clock, "hello world")
    document = editor.active
    bold = editor.styles.get(bold=True)
    document.apply_style(6, 11, bold)
    assert editor.styles.get(bold=True) is bold and len(editor.styles) == 1
    assert document.styles_at(7) == [bold] and document.styles_at(0) == []

    editor.move_to(0)
    editor.type_text(">>")  # inserting before the run pushes it right
    assert document.styles_at(9) == [bold] and document.styles_at(7) == []


def test_observers_see_every_revision(editor: Editor, clock: FakeClock) -> None:
    status = StatusBar()
    editor.active.subscribe(status)
    type_all(editor, clock, "abc")
    editor.undo()
    assert status.updates == 4  # three edits plus the undo
    assert "notes.txt" in status.render()


@pytest.mark.parametrize("factory", [GapBuffer, SimpleBuffer])
def test_both_buffer_strategies_behave_identically(factory) -> None:
    buffer = factory("hello world")
    buffer.insert(5, ",")
    assert buffer.text() == "hello, world" and len(buffer) == 12
    assert buffer.slice(0, 5) == "hello" and buffer.slice(7, 12) == "world"
    assert buffer.delete(5, 2) == ", " and buffer.text() == "helloworld"
    with pytest.raises(OutOfBoundsError):
        buffer.insert(99, "x")


def test_gap_buffer_grows_and_survives_a_caret_that_jumps_around() -> None:
    buffer = GapBuffer("abc", gap_size=2)
    buffer.insert(3, "defghijkl")  # more than the initial gap
    assert buffer.text() == "abcdefghijkl" and buffer.gap_size() > 0
    buffer.insert(0, "-")
    buffer.insert(len(buffer), "+")
    assert buffer.text() == "-abcdefghijkl+"
    assert buffer.delete(0, 1) == "-" and buffer.text() == "abcdefghijkl+"


def test_a_document_used_directly_still_records_commands(clock: FakeClock) -> None:
    document = Document("bare.txt", GapBuffer(), clock)
    before = document.cursor()
    document.run(InsertCommand(0, "hi", before, Cursor(2)))
    assert document.text() == "hi" and document.cursor() == Cursor(2)
    document.undo()
    assert document.text() == "" and document.cursor() == before
