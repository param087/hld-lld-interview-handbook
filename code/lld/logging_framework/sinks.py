"""Where bytes actually land. Injected into ``FileHandler`` so rotation is testable."""

from __future__ import annotations

import os
import threading

from lld.logging_framework.models import Stream


# --8<-- [start:filesystem]
class LocalFileSystem:
    """Production sink factory: real files on the real disk."""

    def open_append(self, path: str) -> Stream:
        # The handler owns the stream lifetime and closes it in close().
        return open(path, "a", encoding="utf-8")

    def size(self, path: str) -> int:
        return os.path.getsize(path) if os.path.exists(path) else 0

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def rename(self, src: str, dst: str) -> None:
        os.replace(src, dst)

    def remove(self, path: str) -> None:
        os.remove(path)


class MemoryStream:
    """A file-like buffer whose contents survive ``close()``, so tests can read them back."""

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self.closed = False

    def write(self, text: str) -> int:
        self._chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def value(self) -> str:
        return "".join(self._chunks)


class MemoryFileSystem:
    """Test and demo sink factory: the same protocol, a dict of buffers."""

    def __init__(self) -> None:
        self._files: dict[str, MemoryStream] = {}
        self._lock = threading.Lock()

    def open_append(self, path: str) -> Stream:
        with self._lock:
            return self._files.setdefault(path, MemoryStream())

    def size(self, path: str) -> int:
        with self._lock:
            return len(self._files[path].value()) if path in self._files else 0

    def exists(self, path: str) -> bool:
        with self._lock:
            return path in self._files

    def rename(self, src: str, dst: str) -> None:
        with self._lock:
            self._files[dst] = self._files.pop(src)

    def remove(self, path: str) -> None:
        with self._lock:
            self._files.pop(path, None)

    def read(self, path: str) -> str:
        with self._lock:
            return self._files[path].value() if path in self._files else ""

    def lines(self, path: str) -> list[str]:
        return [line for line in self.read(path).splitlines() if line]

    def paths(self) -> list[str]:
        with self._lock:
            return sorted(self._files)


# --8<-- [end:filesystem]
