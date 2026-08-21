"""Type, coalesce, select, paste, undo, redo, replace-all, save -- and hit the cap."""

from common import FakeClock
from lld.text_editor.models import NothingToUndoError
from lld.text_editor.services import Editor
from lld.text_editor.support import StatusBar


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    editor = Editor(clock=clock, history_capacity=5, coalesce_window=1.0)
    status = StatusBar()
    document = editor.new_tab("notes.txt")
    document.subscribe(status)
    print(f"new tab: {document.name} status={document.status}")

    for char in "hello":  # five keystrokes inside the coalescing window
        editor.type_text(char)
        clock.advance(0.1)
    print(f"typed 'hello' -> {document.text()!r} as {len(document.history.labels())} undo entry")

    clock.advance(3)  # a pause ends the run
    editor.type_text(" world")
    print(f"after a 3 s pause: {document.history.labels()}")

    editor.undo()
    print(f"undo -> {document.text()!r} (redo available: {document.history.can_redo()})")
    editor.redo()
    print(f"redo -> {document.text()!r}")

    editor.select(0, 5)
    cut = editor.cut()
    print(f"cut {cut!r} -> {document.text()!r}, cursor at {document.cursor().position}")
    editor.move_to(document.length())
    editor.paste()
    print(f"paste at the end -> {document.text()!r}")

    editor.replace_all("l", "L")
    print(f"replace-all -> {document.text()!r} as one entry: {document.history.labels()[-1]}")
    editor.undo()
    print(f"one undo took all three edits back -> {document.text()!r}")
    editor.type_text("!")
    print(f"a new command dropped the redo stack: can_redo={document.history.can_redo()}")

    bold = editor.styles.get(bold=True)
    document.apply_style(0, 5, bold)
    document.apply_style(6, 11, editor.styles.get(bold=True))
    shared = document.style_runs()[0].style is document.style_runs()[1].style
    print(f"two style runs share one flyweight: {shared}, registry holds {len(editor.styles)}")

    editor.save()
    print(f"saved: status={document.status}, words={editor.word_count()}, status bar: {status.render()}")

    for char in "abcdefgh":  # each keystroke separated by a long pause: no coalescing
        editor.type_text(char)
        clock.advance(2)
    kept, dropped = len(document.history.labels()), document.history.dropped
    print(f"cap {document.history.capacity}: {kept + dropped} edits recorded -> {kept} kept, {dropped} dropped")
    while document.history.can_undo():
        editor.undo()
    try:
        editor.undo()
    except NothingToUndoError as exc:
        print(f"undo past the cap: {exc}; the oldest edits are unreachable -> {document.text()!r}")


if __name__ == "__main__":
    main()
