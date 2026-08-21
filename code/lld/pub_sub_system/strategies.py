"""Partitioners. The one policy a producer chooses, and the one that decides ordering."""

from __future__ import annotations

import threading
import zlib


# --8<-- [start:partitioners]
class KeyHashPartitioner:
    """Same key, same partition, forever -- which is what makes per-key ordering work.

    ``zlib.crc32`` and not ``hash()``: CPython salts string hashes per process,
    so ``hash()`` would send the same key to different partitions after a restart.
    """

    def __init__(self, fallback: RoundRobinPartitioner | None = None) -> None:
        self._fallback = fallback or RoundRobinPartitioner()

    def partition_for(self, key: str | None, partition_count: int) -> int:
        if key is None:
            return self._fallback.partition_for(None, partition_count)
        return zlib.crc32(key.encode("utf-8")) % partition_count


class RoundRobinPartitioner:
    """Even spread for keyless messages. The counter needs a lock: many producers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0

    def partition_for(self, key: str | None, partition_count: int) -> int:
        with self._lock:
            chosen = self._next % partition_count
            self._next += 1
            return chosen


class StickyPartitioner:
    """Fills one partition until a batch is full, then moves on -- fewer, larger batches."""

    def __init__(self, batch_size: int = 16) -> None:
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._current = 0
        self._count = 0

    def partition_for(self, key: str | None, partition_count: int) -> int:
        if key is not None:
            return zlib.crc32(key.encode("utf-8")) % partition_count
        with self._lock:
            if self._count >= self.batch_size:
                self._current = (self._current + 1) % partition_count
                self._count = 0
            self._count += 1
            return self._current % partition_count


# --8<-- [end:partitioners]
