"""The log itself: partitions, the offset store and the dead-letter queue.

``Partition`` is the only class in the package that owns a ``Condition``. It is
the producer-consumer rendezvous: producers append and notify, workers wait for
an offset to exist, and commits notify producers that space was reclaimed.
"""

from __future__ import annotations

import threading
from collections import deque

from common import Clock, SystemClock
from lld.pub_sub_system.models import (
    BackpressureError,
    DeadLetter,
    FullPolicy,
    Message,
    OffsetOutOfRangeError,
    Partitioner,
    Record,
    RetentionPolicy,
)
from lld.pub_sub_system.strategies import KeyHashPartitioner


# --8<-- [start:partition]
class Partition:
    """An append-only log segment with a bounded buffer.

    Invariants, all protected by ``_condition``:

    * ``_next_offset`` never goes backwards, so an offset identifies a record forever;
    * ``_records[0].offset == _base_offset``, so index arithmetic replaces a scan;
    * a record is only discarded when every subscribed group has committed past
      it, or when ``max_age_seconds`` says it is stale.
    """

    def __init__(self, topic: str, index: int, retention: RetentionPolicy, clock: Clock | None = None) -> None:
        self.topic = topic
        self.index = index
        self.retention = retention
        self._clock = clock or SystemClock()
        self._condition = threading.Condition()
        self._records: deque[Record] = deque()
        self._base_offset = 0
        self._next_offset = 0
        self._low_water = 0  # the oldest offset any subscribed group still needs
        self._closed = False
        self.dropped = 0  # records lost to DROP_OLDEST or to age-based retention

    def append(self, message: Message) -> Record:
        """Producer side. Blocks (or sheds) when the buffer is full, then notifies workers."""
        with self._condition:
            if self._closed:
                raise BackpressureError(f"{self.topic}/{self.index} is closed")
            self._trim_expired_locked()
            if len(self._records) >= self.retention.max_messages:
                self._make_room_locked()
            record = Record(self.topic, self.index, self._next_offset, message)
            self._records.append(record)
            self._next_offset += 1
            self._condition.notify_all()  # wake every worker parked on this partition
            return record

    def _make_room_locked(self) -> None:
        """Reclaim what every group has acked; if that is not enough, block or shed."""
        if self._has_room_locked():
            return
        if self.retention.on_full is FullPolicy.DROP_OLDEST:
            self._records.popleft()
            self._base_offset += 1
            self.dropped += 1
            return
        room = self._condition.wait_for(
            lambda: self._closed or self._has_room_locked(),
            timeout=self.retention.block_timeout,
        )
        if not room:
            raise BackpressureError(
                f"{self.topic}/{self.index} full ({self.retention.max_messages}) "
                f"after {self.retention.block_timeout}s: the slowest group is not committing"
            )

    def _has_room_locked(self) -> bool:
        self._reclaim_locked()
        return len(self._records) < self.retention.max_messages

    def _reclaim_locked(self) -> None:
        """Drop only records that every subscribed group has committed past.

        Records are *not* freed the moment they are acked: keeping them until
        the buffer is under pressure is what makes replay possible at all.
        """
        while self._records and self._records[0].offset < self._low_water:
            self._records.popleft()
            self._base_offset += 1

    def fetch(self, offset: int, timeout: float, stop: threading.Event | None = None) -> Record | None:
        """Consumer side. Waits for ``offset`` to exist; ``None`` means timeout or shutdown."""
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._closed or offset < self._next_offset or (stop is not None and stop.is_set()),
                timeout=timeout,
            )
            if not ready or (stop is not None and stop.is_set()) or offset >= self._next_offset:
                return None
            if offset < self._base_offset:
                raise OffsetOutOfRangeError(
                    f"offset {offset} was trimmed from {self.topic}/{self.index}; "
                    f"earliest is {self._base_offset}"
                )
            return self._records[offset - self._base_offset]

    def set_low_water(self, offset: int) -> None:
        """Record how far the slowest group has got, and wake any blocked producer."""
        with self._condition:
            self._low_water = max(self._low_water, min(offset, self._next_offset))
            self._condition.notify_all()

    def _trim_expired_locked(self) -> None:
        if self.retention.max_age_seconds is None:
            return
        cutoff = self._clock.now() - self.retention.max_age_seconds
        while self._records and self._records[0].message.created < cutoff:
            self._records.popleft()
            self._base_offset += 1
            self.dropped += 1

    def wake(self) -> None:
        """Used by shutdown and rebalancing to unpark workers immediately."""
        with self._condition:
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def earliest_offset(self) -> int:
        with self._condition:
            return self._base_offset

    def next_offset(self) -> int:
        with self._condition:
            return self._next_offset

    def size(self) -> int:
        with self._condition:
            return len(self._records)

    def low_water(self) -> int:
        with self._condition:
            return self._low_water


# --8<-- [end:partition]


# --8<-- [start:topic]
class Topic:
    """A named set of partitions plus the partitioner that routes into them."""

    def __init__(
        self,
        name: str,
        partition_count: int = 1,
        retention: RetentionPolicy | None = None,
        clock: Clock | None = None,
        partitioner: Partitioner | None = None,
    ) -> None:
        self.name = name
        self.retention = retention or RetentionPolicy()
        self._partitioner: Partitioner = partitioner or KeyHashPartitioner()
        self._partitions = [Partition(name, i, self.retention, clock) for i in range(partition_count)]

    @property
    def partition_count(self) -> int:
        return len(self._partitions)

    def partitions(self) -> list[Partition]:
        return list(self._partitions)

    def partition(self, index: int) -> Partition:
        return self._partitions[index]

    def route(self, key: str | None) -> Partition:
        return self._partitions[self._partitioner.partition_for(key, len(self._partitions))]

    def close(self) -> None:
        for partition in self._partitions:
            partition.close()


# --8<-- [end:topic]


# --8<-- [start:offsets]
class OffsetStore:
    """Committed offsets keyed by ``(group, topic, partition)``.

    The committed value is the offset of the *next* record to read, so an empty
    entry and "start from the beginning" are the same thing: zero.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._offsets: dict[tuple[str, str, int], int] = {}

    def committed(self, group: str, topic: str, partition: int) -> int:
        with self._lock:
            return self._offsets.get((group, topic, partition), 0)

    def seek(self, group: str, topic: str, partition: int, offset: int) -> None:
        """Unconditional move, used for replay while the group is paused."""
        with self._lock:
            self._offsets[(group, topic, partition)] = max(0, offset)

    def advance(self, group: str, topic: str, partition: int, expected: int, new: int) -> bool:
        """Compare-and-set: a concurrent seek must not be clobbered by a late ack."""
        key = (group, topic, partition)
        with self._lock:
            if self._offsets.get(key, 0) != expected:
                return False
            self._offsets[key] = new
            return True

    def low_water(self, topic: str, partition: int, groups: list[str]) -> int:
        """The oldest offset any group still needs. Below it, records can be dropped."""
        if not groups:
            return 0  # no subscribers yet: keep everything so a late group can replay
        with self._lock:
            return min(self._offsets.get((group, topic, partition), 0) for group in groups)

    def snapshot(self) -> dict[tuple[str, str, int], int]:
        with self._lock:
            return dict(self._offsets)


# --8<-- [end:offsets]


# --8<-- [start:dlq]
class DeadLetterQueue:
    """Where poison messages go so the partition can keep moving."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._letters: list[DeadLetter] = []

    def add(self, letter: DeadLetter) -> None:
        with self._lock:
            self._letters.append(letter)

    def letters(self, group: str | None = None) -> list[DeadLetter]:
        with self._lock:
            return [dl for dl in self._letters if group is None or dl.group == group]

    def __len__(self) -> int:
        with self._lock:
            return len(self._letters)


# --8<-- [end:dlq]
