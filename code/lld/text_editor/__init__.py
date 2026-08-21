"""A text editor: gap buffer, Command undo/redo with coalescing, tabs and styles."""

from lld.text_editor.buffers import GapBuffer, SimpleBuffer
from lld.text_editor.commands import (
    Command,
    CommandHistory,
    DeleteCommand,
    EditTarget,
    InsertCommand,
    MacroCommand,
    ReplaceCommand,
)
from lld.text_editor.models import (
    Cursor,
    Direction,
    DocumentExistsError,
    DocumentListener,
    DocumentNotFoundError,
    DocumentStatus,
    EmptyClipboardError,
    FileStorage,
    NothingToUndoError,
    OutOfBoundsError,
    Selection,
    Style,
    StyleRun,
    TextBuffer,
)
from lld.text_editor.services import Document, Editor
from lld.text_editor.support import Clipboard, InMemoryStorage, StatusBar, StyleRegistry

__all__ = [
    "Clipboard",
    "Command",
    "CommandHistory",
    "Cursor",
    "DeleteCommand",
    "Direction",
    "Document",
    "DocumentExistsError",
    "DocumentListener",
    "DocumentNotFoundError",
    "DocumentStatus",
    "EditTarget",
    "Editor",
    "EmptyClipboardError",
    "FileStorage",
    "GapBuffer",
    "InMemoryStorage",
    "InsertCommand",
    "MacroCommand",
    "NothingToUndoError",
    "OutOfBoundsError",
    "ReplaceCommand",
    "Selection",
    "SimpleBuffer",
    "StatusBar",
    "Style",
    "StyleRegistry",
    "StyleRun",
    "TextBuffer",
]
