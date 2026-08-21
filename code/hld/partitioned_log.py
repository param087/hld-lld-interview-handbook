"""Segmented partition log and ISR replication: the two halves of a Kafka-style broker.

What the module demonstrates, in the order an interviewer asks about it:

* ``Segment`` is one log file: records appended in offset order plus a *sparse* index with one
  ``(offset, byte position)`` entry per ``index_interval`` bytes, so a fetch is a bisect over a
  small array and a short forward scan instead of a scan of the whole file.
* ``SegmentedLog`` rolls a new segment when the active one is full, which turns retention into
  a file deletion (``expire``) instead of a rewrite, and turns a follower's post-election
  rollback into a truncation of the tail (``truncate``).
* ``ReplicatedPartition`` runs the leader/follower protocol: followers pull with ``replicate``,
  the high watermark is the lowest log end offset in the in-sync replica set, consumers never
  read above it, ``acks`` decides when the producer is answered, and ``fail`` elects a new
  leader and reports how many acknowledged records that choice threw away.

Consumer groups, offset commits and the idempotent producer are *not* rebuilt here: they live
in ``hld.mini_kafka`` and the demo drives them through ``Broker``, ``Producer`` and
``ConsumerGroup``.
"""

from __future__ import annotations

import bisect
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from common import (
    Clock,
    FakeClock,
    InvalidStateError,
    NotFoundError,
    SystemClock,
    ValidationError,
)
from hld.mini_kafka import Broker, ConsumerGroup, OffsetOutOfRangeError, Producer, Record

RECORD_HEADER_BYTES = 32  # offset, timestamp, key and value lengths, CRC
DEFAULT_SEGMENT_BYTES = 1 << 30  # Kafka's segment.bytes default: 1 GB
DEFAULT_INDEX_INTERVAL = 4096  # Kafka's index.interval.bytes default


def record_bytes(key: str | None, value: str | None) -> int:
    """On-disk size of one record: a fixed header plus the payload."""
    return RECORD_HEADER_BYTES + len(key or "") + len(value or "")


# --8<-- [start:segment]
@dataclass(frozen=True, slots=True)
class SegmentInfo:
    """What ``ls`` on a partition directory would tell you about one segment."""

    base_offset: int
    records: int
    size_bytes: int
    index_entries: int


class Segment:
    """One segment file plus the sparse index into it.

    Records are written at increasing byte positions and never moved. ``_index`` keeps one
    ``(offset, position)`` pair per ``index_interval`` bytes, so the index of a 1 GB segment
    costs a few hundred KB instead of one entry per record; a lookup bisects it and scans
    forward from the entry it lands on. Not thread-safe on its own: the owning ``SegmentedLog``
    serialises every call with its lock.
    """

    def __init__(self, base_offset: int, index_interval: int = DEFAULT_INDEX_INTERVAL) -> None:
        if index_interval <= 0:
            raise ValidationError("index_interval must be positive")
        self.base_offset = base_offset
        self.size_bytes = 0
        self._index_interval = index_interval
        self._records: list[Record] = []
        self._positions: list[int] = []  # byte position of every record
        self._index: list[tuple[int, int]] = []  # sparse: (offset, byte position)
        self._since_index = index_interval  # force an index entry for the first record

    @property
    def end_offset(self) -> int:
        """One past the last offset in this segment; the base offset of the next one."""
        return self.base_offset + len(self._records)

    @property
    def max_timestamp(self) -> float:
        """Timestamp of the newest record; retention compares whole segments against it."""
        return self._records[-1].timestamp if self._records else 0.0

    def append(self, record: Record) -> None:
        size = record_bytes(record.key, record.value)
        if self._since_index >= self._index_interval:
            self._index.append((record.offset, self.size_bytes))
            self._since_index = 0
        self._positions.append(self.size_bytes)
        self._records.append(record)
        self.size_bytes += size
        self._since_index += size

    def lookup(self, offset: int) -> tuple[int, int]:
        """``(byte position, records scanned)``: bisect the sparse index, then scan forward."""
        if not self.base_offset <= offset < self.end_offset:
            raise OffsetOutOfRangeError(f"offset {offset} is not in segment {self.base_offset}")
        i = bisect.bisect_right(self._index, offset, key=lambda entry: entry[0]) - 1
        return self._positions[offset - self.base_offset], offset - self._index[i][0]

    def read(self, offset: int, limit: int) -> list[Record]:
        start = max(offset - self.base_offset, 0)
        return self._records[start : start + limit]

    def truncate(self, offset: int) -> int:
        """Drop every record at or after ``offset``; returns how many were dropped."""
        keep = max(offset - self.base_offset, 0)
        dropped = len(self._records) - keep
        if dropped <= 0:
            return 0
        self._records = self._records[:keep]
        self._positions = self._positions[:keep]
        self.size_bytes = sum(record_bytes(r.key, r.value) for r in self._records)
        self._index = [entry for entry in self._index if entry[0] < offset]
        self._since_index = self._index_interval  # the next append re-indexes
        return dropped

    def info(self) -> SegmentInfo:
        return SegmentInfo(self.base_offset, len(self._records), self.size_bytes, len(self._index))


# --8<-- [end:segment]


# --8<-- [start:log]
class SegmentedLog:
    """The log of one partition: closed segments plus one active segment open for appends.

    ``_lock`` guards the segment list and therefore the log end offset and the log start
    offset. A real broker serves fetches straight from the page cache without touching the
    writer's lock; one lock here keeps the demo honest without pretending to be a broker.
    """

    def __init__(
        self,
        topic: str,
        partition: int,
        *,
        segment_bytes: int = DEFAULT_SEGMENT_BYTES,
        index_interval: int = DEFAULT_INDEX_INTERVAL,
    ) -> None:
        if segment_bytes <= 0:
            raise ValidationError("segment_bytes must be positive")
        self.topic = topic
        self.partition = partition
        self._segment_bytes = segment_bytes
        self._index_interval = index_interval
        self._segments: list[Segment] = [Segment(0, index_interval)]
        self._lock = threading.Lock()

    @property
    def end_offset(self) -> int:
        """The log end offset (LEO): the offset the next appended record will get."""
        with self._lock:
            return self._segments[-1].end_offset

    @property
    def log_start_offset(self) -> int:
        """The oldest readable offset; retention moves it forward one whole segment at a time."""
        with self._lock:
            return self._segments[0].base_offset

    def append(self, key: str | None, value: str | None, timestamp: float) -> Record:
        """Append one record, rolling a new segment when the active one is full.

        This is the only write the broker makes: one sequential append to the end of one file,
        which is why a spinning disk that manages 100 random IOPS still sustains 150 MB/s here.
        """
        with self._lock:
            active = self._segments[-1]
            if active.size_bytes and active.size_bytes + record_bytes(key, value) > self._segment_bytes:
                active = Segment(active.end_offset, self._index_interval)
                self._segments.append(active)
            record = Record(self.topic, self.partition, active.end_offset, key, value, timestamp)
            active.append(record)
            return record

    def read(self, offset: int, limit: int = 100) -> list[Record]:
        """Records from ``offset`` onwards, crossing segment boundaries if the batch spans them."""
        if limit <= 0:
            raise ValidationError("limit must be positive")
        with self._lock:
            if offset < self._segments[0].base_offset:
                raise OffsetOutOfRangeError(
                    f"offset {offset} is below the log start offset {self._segments[0].base_offset}"
                )
            out: list[Record] = []
            for segment in self._segments[self._segment_index(offset) :]:
                out.extend(segment.read(offset, limit - len(out)))
                if len(out) >= limit:
                    break
            return out

    def lookup(self, offset: int) -> tuple[int, int, int]:
        """``(segment base offset, byte position, records scanned)``: how a fetch finds a record."""
        with self._lock:
            if not self._segments[0].base_offset <= offset < self._segments[-1].end_offset:
                raise OffsetOutOfRangeError(f"offset {offset} is outside the log")
            segment = self._segments[self._segment_index(offset)]
            position, scanned = segment.lookup(offset)
            return segment.base_offset, position, scanned

    def expire(self, before: float) -> int:
        """Time retention: delete whole *closed* segments whose newest record predates ``before``.

        Deleting a file is O(1) and touches no reader; Kafka never rewrites a segment to drop
        individual records, which is why retention costs nothing at any throughput.
        """
        with self._lock:
            deleted = 0
            while len(self._segments) > 1 and self._segments[0].max_timestamp < before:
                self._segments.pop(0)
                deleted += 1
            return deleted

    def truncate(self, offset: int) -> int:
        """Drop everything at or after ``offset`` (a replica rolling back after an election)."""
        with self._lock:
            if offset < self._segments[0].base_offset:
                raise ValidationError(f"cannot truncate below the log start offset {offset}")
            dropped = 0
            while len(self._segments) > 1 and self._segments[-1].base_offset >= offset:
                dropped += self._segments.pop().info().records
            return dropped + self._segments[-1].truncate(offset)

    def segments(self) -> list[SegmentInfo]:
        with self._lock:
            return [segment.info() for segment in self._segments]

    def _segment_index(self, offset: int) -> int:
        """Which segment holds ``offset``: a bisect over the base offsets, O(log segments)."""
        i = bisect.bisect_right(self._segments, offset, key=lambda s: s.base_offset) - 1
        return max(i, 0)


# --8<-- [end:log]


# --8<-- [start:isr]
class Acks(StrEnum):
    """When the leader answers the producer."""

    NONE = "0"  # fire and forget: no ack at all
    LEADER = "1"  # the leader's log has it
    ALL = "all"  # every in-sync replica has it


class NotEnoughReplicasError(InvalidStateError):
    """``acks=all`` cannot be honoured: the ISR is below ``min.insync.replicas``."""


@dataclass(frozen=True, slots=True)
class LeaderChange:
    old_leader: str
    new_leader: str
    lost_records: int  # acknowledged on the old leader, absent from the new one


class ReplicatedPartition:
    """One partition replicated across a leader and its followers.

    Followers *pull*: ``replicate`` copies records from the leader's log into a follower's own
    log, exactly as a Kafka follower fetches. The **high watermark** is the lowest log end
    offset in the in-sync replica set, and ``fetch`` refuses to return anything above it,
    because a record that only the leader holds can still vanish in an election.

    ``_lock`` (re-entrant, because ``produce`` replicates while holding it) guards the leader
    name, the ISR, the down set and the per-follower catch-up times; each replica's
    ``SegmentedLog`` has its own lock.
    """

    def __init__(
        self,
        topic: str,
        partition: int,
        replicas: Sequence[str],
        *,
        min_insync: int = 2,
        max_lag: float = 10.0,
        segment_bytes: int = DEFAULT_SEGMENT_BYTES,
        clock: Clock | None = None,
    ) -> None:
        if len(set(replicas)) < 2 or len(set(replicas)) != len(replicas):
            raise ValidationError("a replicated partition needs at least two distinct replicas")
        if not 1 <= min_insync <= len(replicas):
            raise ValidationError("min_insync must be between 1 and the replication factor")
        self.topic = topic
        self.partition = partition
        self._clock = clock or SystemClock()
        self._min_insync = min_insync
        self._max_lag = max_lag
        self._logs = {
            node: SegmentedLog(topic, partition, segment_bytes=segment_bytes) for node in replicas
        }
        self._leader = replicas[0]
        self._isr: set[str] = set(replicas)
        self._down: set[str] = set()
        self._caught_up = {node: self._clock.now() for node in replicas}
        self._lock = threading.RLock()

    @property
    def leader(self) -> str:
        with self._lock:
            return self._leader

    @property
    def isr(self) -> list[str]:
        with self._lock:
            return sorted(self._isr)

    @property
    def high_watermark(self) -> int:
        """The lowest log end offset in the ISR: below it, every in-sync replica has the record."""
        with self._lock:
            return min((self._logs[node].end_offset for node in self._isr), default=0)

    def log_end_offset(self, node: str) -> int:
        return self._log(node).end_offset

    def produce(self, key: str | None, value: str | None, acks: Acks = Acks.ALL) -> Record:
        """Append to the leader and answer the producer according to ``acks``.

        ``acks=all`` is refused outright when the ISR is below ``min.insync.replicas``: the
        broker would rather fail the send than acknowledge a write that one disk can erase.
        """
        with self._lock:
            if acks is Acks.ALL and len(self._isr) < self._min_insync:
                raise NotEnoughReplicasError(
                    f"isr {sorted(self._isr)} is below min.insync.replicas={self._min_insync}"
                )
            record = self._logs[self._leader].append(key, value, self._clock.now())
            if acks is Acks.ALL:
                for node in sorted(self._isr - {self._leader}):
                    self._replicate(node)
            return record

    def replicate(self, node: str | None = None) -> int:
        """One follower fetch, or every healthy follower's fetch; returns records copied.

        Reaching the leader's log end offset is what keeps a follower in the ISR and what puts
        it back after it fell out.
        """
        with self._lock:
            targets = (
                [n for n in sorted(self._logs) if n != self._leader and n not in self._down]
                if node is None
                else [node]
            )
            return sum(self._replicate(n) for n in targets)

    def fetch(self, offset: int, limit: int = 100) -> list[Record]:
        """A consumer read from the leader, clamped to the high watermark."""
        with self._lock:
            available = self.high_watermark - offset
            if available < 0:
                raise ValidationError(f"offset {offset} is above the high watermark")
            return self._logs[self._leader].read(offset, min(limit, available)) if available else []

    def check_isr(self) -> list[str]:
        """Shrink the ISR: evict followers that have not caught up within ``max_lag`` seconds.

        Without this a single stalled replica would pin the high watermark and stall every
        ``acks=all`` producer behind it.
        """
        with self._lock:
            now = self._clock.now()
            removed = sorted(
                node
                for node in self._isr
                if node != self._leader and now - self._caught_up[node] > self._max_lag
            )
            self._isr.difference_update(removed)
            return removed

    def fail(self, node: str) -> LeaderChange | None:
        """Take a replica down.

        A follower simply leaves the ISR. When the leader dies, the in-sync replica with the
        highest log end offset is elected and every record the old leader acknowledged above
        that offset is gone: exactly the records ``acks=1`` had already confirmed.
        """
        with self._lock:
            self._log(node)
            self._down.add(node)
            self._isr.discard(node)
            if node != self._leader:
                return None
            if not self._isr:
                raise InvalidStateError(f"{self.topic}-{self.partition} is offline: empty ISR")
            new_leader = max(sorted(self._isr), key=lambda n: self._logs[n].end_offset)
            lost = self._logs[node].end_offset - self._logs[new_leader].end_offset
            old, self._leader = self._leader, new_leader
            self._caught_up[new_leader] = self._clock.now()
            return LeaderChange(old, new_leader, lost)

    def recover(self, node: str) -> int:
        """Bring a replica back: truncate its unreplicated tail, then fetch from the leader."""
        with self._lock:
            self._down.discard(node)
            log = self._log(node)
            watermark = self.high_watermark
            dropped = log.truncate(watermark) if log.end_offset > watermark else 0
            self._caught_up[node] = self._clock.now()
            self._replicate(node)
            return dropped

    def describe(self) -> str:
        with self._lock:
            leos = " ".join(f"{n}={self._logs[n].end_offset}" for n in sorted(self._logs))
            return f"leader={self._leader} isr={sorted(self._isr)} hw={self.high_watermark} leo: {leos}"

    def _log(self, node: str) -> SegmentedLog:
        if node not in self._logs:
            raise NotFoundError(f"{node!r} is not a replica of {self.topic}-{self.partition}")
        return self._logs[node]

    def _replicate(self, node: str) -> int:
        """Copy the leader's tail into one follower's log. Caller holds ``_lock``."""
        log, leader_log = self._log(node), self._logs[self._leader]
        if node in self._down or node == self._leader:
            return 0
        copied = 0
        while log.end_offset < leader_log.end_offset:
            batch = leader_log.read(log.end_offset, 100)
            if not batch:
                break
            for record in batch:
                log.append(record.key, record.value, record.timestamp)
                copied += 1
        if log.end_offset == leader_log.end_offset:
            self._caught_up[node] = self._clock.now()
            self._isr.add(node)
        return copied


# --8<-- [end:isr]


def main() -> None:
    clock = FakeClock(start=1_700_000_000.0)

    log = SegmentedLog("orders", 0, segment_bytes=4_096, index_interval=512)
    for i in range(500):
        log.append(f"user:{i % 50}", f"order-{i:04d}", clock.now())
        clock.advance(1)
    infos = log.segments()
    print(
        f"500 records of {record_bytes('user:7', 'order-0000')} B into 4 KB segments: "
        f"{len(infos)} segments, bases {[s.base_offset for s in infos[:4]]}..."
    )
    base, position, scanned = log.lookup(321)
    held_by = next(s for s in infos if s.base_offset == base)
    print(
        f"lookup offset 321        : segment {base} ({held_by.index_entries} index entries for "
        f"{held_by.records} records), byte {position}, {scanned} records scanned after the bisect"
    )
    deleted = log.expire(clock.now() - 300)
    print(
        f"retention, keep 300 s    : deleted {deleted} whole segments, log start offset 0 -> "
        f"{log.log_start_offset}, {len(log.segments())} segments left"
    )

    part = ReplicatedPartition("orders", 0, ["n1", "n2", "n3"], min_insync=2, clock=clock)
    for i in range(3):
        part.produce("ann", f"order-{i}", acks=Acks.ALL)
    print(f"produce x3 acks=all      : {part.describe()}")
    clock.advance(15)  # n3 stops fetching for 15 s; n2 keeps up
    part.replicate("n2")
    print(f"n3 stalls 15 s > max_lag : evicted {part.check_isr()} -> {part.describe()}")
    part.produce("bob", "order-3", acks=Acks.ALL)
    print(f"produce acks=all, isr=2  : accepted, hw={part.high_watermark} (min.insync.replicas=2)")
    for i in (4, 5):
        part.produce("bob", f"order-{i}", acks=Acks.LEADER)
    print(
        f"produce x2 acks=1        : leader leo={part.log_end_offset('n1')}, "
        f"hw={part.high_watermark}, consumers see {len(part.fetch(0))} records"
    )
    change = part.fail("n1")
    print(
        f"leader n1 fails          : {change.old_leader} -> {change.new_leader}, "
        f"{change.lost_records} acknowledged records lost (they were above the high watermark)"
    )
    try:
        part.produce("bob", "order-6", acks=Acks.ALL)
    except NotEnoughReplicasError as exc:
        print(f"produce acks=all, isr=1  : refused: {exc}")
    print(f"n1 returns               : truncated {part.recover('n1')} records -> {part.describe()}")

    broker = Broker(clock=clock)
    broker.create_topic("payments", partitions=3)
    producer = Producer(broker, producer_id="checkout-1")
    for i, user in enumerate(["ann", "bob", "ann", "cid", "bob", "ann"]):
        producer.send("payments", value=f"pay-{i}", key=user)
    group = ConsumerGroup(broker, "billing", "payments")
    group.join("c1")
    group.join("c2")
    print(f"consumer group           : c1={group.assignment('c1')} c2={group.assignment('c2')}")
    print(f"c1 polls {len(group.poll('c1'))}, commits {group.commit('c1')}, lag now {group.lag()}")


if __name__ == "__main__":
    main()
