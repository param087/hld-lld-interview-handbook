"""The small collaborators: clipboard, style flyweights, storage and a view."""

from __future__ import annotations

import threading
from typing import ClassVar

from lld.text_editor.models import DocumentNotFoundError, EmptyClipboardError, Style


# --8<-- [start:clipboard]
class Clipboard:
    """One system clipboard per process, but injectable so tests never share one.

    ``instance()`` exists because the clipboard genuinely is a machine-wide
    resource; the public constructor exists because a Singleton you cannot
    replace is a Singleton you cannot test.
    """

    _instance_lock: ClassVar[threading.Lock] = threading.Lock()
    _instance: ClassVar[Clipboard | None] = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._content: str | None = None

    @classmethod
    def instance(cls) -> Clipboard:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def copy(self, text: str) -> None:
        with self._lock:
            self._content = text

    def paste(self) -> str:
        with self._lock:
            if self._content is None:
                raise EmptyClipboardError("clipboard is empty")
            return self._content

    def is_empty(self) -> bool:
        with self._lock:
            return self._content is None


# --8<-- [end:clipboard]


# --8<-- [start:styles]
class StyleRegistry:
    """Flyweight factory: one shared ``Style`` object per distinct combination.

    A million bold characters cost one Style plus one run, not a million objects.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._styles: dict[tuple[bool, bool, bool, str], Style] = {}

    def get(
        self, bold: bool = False, italic: bool = False, underline: bool = False, colour: str = "default"
    ) -> Style:
        key = (bold, italic, underline, colour)
        with self._lock:
            style = self._styles.get(key)
            if style is None:
                style = Style(*key)
                self._styles[key] = style
            return style

    def __len__(self) -> int:
        with self._lock:
            return len(self._styles)


# --8<-- [end:styles]


class InMemoryStorage:
    """A ``FileStorage`` that lives in a dict. Swap for a real path in production."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: dict[str, str] = {}

    def save(self, name: str, content: str) -> None:
        with self._lock:
            self._files[name] = content

    def load(self, name: str) -> str:
        with self._lock:
            try:
                return self._files[name]
            except KeyError:
                raise DocumentNotFoundError(f"no stored document named {name!r}") from None

    def exists(self, name: str) -> bool:
        with self._lock:
            return name in self._files

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._files)


class StatusBar:
    """Observer: a view that redraws when the active document changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.updates = 0
        self._last = ("", 0)

    def on_document_changed(self, name: str, revision: int) -> None:
        with self._lock:
            self.updates += 1
            self._last = (name, revision)

    def render(self) -> str:
        with self._lock:
            name, revision = self._last
            return f"{name} (rev {revision}, {self.updates} redraws)"
