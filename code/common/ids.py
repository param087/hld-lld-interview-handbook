"""Injectable ID generators (deterministic in tests, random in production)."""

from __future__ import annotations

import itertools
import threading
import uuid
from typing import Protocol


class IdGenerator(Protocol):
    def next_id(self) -> str: ...


class SequentialIdGenerator:
    """``prefix-1``, ``prefix-2``, ... Thread-safe; ideal for tests and demos."""

    def __init__(self, prefix: str = "id", start: int = 1) -> None:
        self._prefix = prefix
        self._counter = itertools.count(start)
        self._lock = threading.Lock()

    def next_id(self) -> str:
        with self._lock:
            return f"{self._prefix}-{next(self._counter)}"


class UuidIdGenerator:
    """Random 128-bit IDs for production use."""

    def next_id(self) -> str:
        return uuid.uuid4().hex
