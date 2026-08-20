"""Injectable clocks.

Services never call ``time.time()`` directly; they take a ``Clock`` so tests can
control time deterministically with ``FakeClock``.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Anything that can tell the time."""

    def now(self) -> float:
        """Seconds since the Unix epoch (float)."""
        ...

    def now_dt(self) -> datetime:
        """Current time as an aware UTC ``datetime``."""
        ...


class SystemClock:
    """Real wall-clock time."""

    def now(self) -> float:
        return time.time()

    def now_dt(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A clock you move by hand. Thread-safe.

    >>> clock = FakeClock(start=100.0)
    >>> clock.advance(5)
    >>> clock.now()
    105.0
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def now_dt(self) -> datetime:
        return datetime.fromtimestamp(self.now(), tz=UTC)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move a clock backwards; use set()")
        with self._lock:
            self._now += seconds

    def set(self, timestamp: float) -> None:
        with self._lock:
            self._now = float(timestamp)
