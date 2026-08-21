"""Record predicates: sampling, rate limiting and subtree selection.

A filter is finer than a level. A level answers "how bad is this?"; a filter
answers "do I want this particular record on this particular sink?".
"""

from __future__ import annotations

import random
import threading

from common import Clock, SystemClock
from lld.logging_framework.models import LogLevel, LogRecord


# --8<-- [start:filters]
class LevelRangeFilter:
    """Keep only records inside a window, e.g. an audit sink that wants WARNING..ERROR."""

    def __init__(self, low: LogLevel, high: LogLevel = LogLevel.CRITICAL) -> None:
        self.low, self.high = low, high

    def allows(self, record: LogRecord) -> bool:
        return self.low <= record.level <= self.high


class NamePrefixFilter:
    """Keep records from one subtree; ``exclude=True`` inverts it."""

    def __init__(self, prefix: str, exclude: bool = False) -> None:
        self.prefix, self.exclude = prefix, exclude

    def allows(self, record: LogRecord) -> bool:
        hit = record.logger_name == self.prefix or record.logger_name.startswith(self.prefix + ".")
        return not hit if self.exclude else hit


class RateLimitFilter:
    """Noisy-log protection: at most N records per logger per window.

    Uses the injected clock, so a test proves the reset without sleeping.
    """

    def __init__(self, max_per_window: int, window_seconds: float, clock: Clock | None = None) -> None:
        self.max_per_window, self.window_seconds = max_per_window, window_seconds
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int]] = {}
        self.suppressed = 0

    def allows(self, record: LogRecord) -> bool:
        now = self._clock.now()
        with self._lock:
            start, count = self._windows.get(record.logger_name, (now, 0))
            if now - start >= self.window_seconds:
                start, count = now, 0
            if count >= self.max_per_window:
                self.suppressed += 1
                return False
            self._windows[record.logger_name] = (start, count + 1)
            return True


class SamplingFilter:
    """Keep a deterministic fraction of DEBUG-style chatter; errors always pass."""

    def __init__(self, rate: float, seed: int = 42, always_at: LogLevel = LogLevel.WARNING) -> None:
        self.rate, self.always_at = rate, always_at
        self._random = random.Random(seed)
        self._lock = threading.Lock()

    def allows(self, record: LogRecord) -> bool:
        if record.level >= self.always_at:
            return True
        with self._lock:
            return self._random.random() < self.rate


# --8<-- [end:filters]
