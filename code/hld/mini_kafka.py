"""An in-memory Kafka: partitioned append-only log, idempotent producer, consumer groups.

* ``Producer`` sends a keyed record to ``hash(key) % partitions`` of a ``Broker`` topic (order
  holds per partition, never per topic) and numbers it, so the broker can drop a retried send.
* ``ConsumerGroup`` gives every partition to one member and commits offsets to the broker; a
  member that leaves without committing hands its uncommitted records to the survivors.
* ``Broker.compact`` keeps the newest record per key; ``Broker.expire`` applies time retention.
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import threading
from dataclasses import dataclass

from common import (
    Clock,
    ConflictError,
    FakeClock,
    InvalidStateError,
    NotFoundError,
    SystemClock,
    ValidationError,
)


class OffsetOutOfRangeError(InvalidStateError):
    """The requested offset is below the log start offset (retention deleted it)."""


# --8<-- [start:log]
def partition_hash(key: str) -> int:
    """Stable partitioner hash. ``hash()`` is salted per process, so two producers would
    disagree on a key's partition and per-key ordering would be lost."""
    digest = hashlib.md5(key.encode(), usedforsecurity=False).digest()
    return int.from_bytes(digest[:4], "big")


@dataclass(frozen=True, slots=True)
class Record:
    topic: str
    partition: int
    offset: int
    key: str | None
    value: str | None  # ``None`` is a tombstone: compaction deletes the key
    timestamp: float


class Partition:
    """One append-only log; offsets are never renumbered (compaction and retention leave gaps,
    so ``read`` bisects). The owning ``Broker`` serialises every call with its lock."""

    def __init__(self, topic: str, index: int) -> None:
        self.topic = topic
        self.index = index
        self._records: list[Record] = []
        self.end_offset = 0  # the offset the next record gets; for readers, the high watermark
        self.log_start_offset = 0  # oldest readable offset; retention moves it forward

    def append(self, key: str | None, value: str | None, timestamp: float) -> Record:
        record = Record(self.topic, self.index, self.end_offset, key, value, timestamp)
        self._records.append(record)
        self.end_offset += 1
        return record

    def read(self, offset: int, limit: int) -> list[Record]:
        if offset < self.log_start_offset:
            raise OffsetOutOfRangeError(f"offset {offset} is below {self.log_start_offset}")
        start = bisect.bisect_left(self._records, offset, key=lambda r: r.offset)
        return self._records[start : start + limit]

    def compact(self) -> int:
        """Keep the newest record per key; unkeyed records and tombstones stay (Kafka drops
        tombstones only after a grace period, so late readers still see the delete)."""
        newest = {r.key: r.offset for r in self._records if r.key is not None}
        keep = [r for r in self._records if r.key is None or newest[r.key] == r.offset]
        removed, self._records = len(self._records) - len(keep), keep
        return removed

    def expire(self, before: float) -> int:
        """Delete records older than ``before`` (Kafka deletes whole segments from the head)."""
        keep = [r for r in self._records if r.timestamp >= before]
        removed, self._records = len(self._records) - len(keep), keep
        self.log_start_offset = keep[0].offset if keep else self.end_offset
        return removed


# --8<-- [end:log]
# --8<-- [start:broker]
class Broker:
    """The cluster collapsed into one process. ``_lock`` guards the topic map, every
    partition's list, the committed offsets and the producer sequence table (a real broker
    locks per partition log and serves fetches from the page cache)."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._topics: dict[str, list[Partition]] = {}
        self._retention: dict[str, float | None] = {}
        # (group, topic, partition) -> next offset the group will read
        self._committed: dict[tuple[str, str, int], int] = {}
        # (producer_id, topic, partition) -> (last sequence, the record it produced)
        self._sequences: dict[tuple[str, str, int], tuple[int, Record]] = {}
        self._lock = threading.Lock()

    def create_topic(self, name: str, partitions: int = 3, retention: float | None = None) -> None:
        if partitions <= 0:
            raise ValidationError("a topic needs at least one partition")
        if retention is not None and retention <= 0:
            raise ValidationError("retention must be positive")
        with self._lock:
            if name in self._topics:
                raise ConflictError(f"topic {name!r} exists")
            self._topics[name] = [Partition(name, i) for i in range(partitions)]
            self._retention[name] = retention

    def _logs(self, topic: str) -> list[Partition]:
        if topic not in self._topics:
            raise NotFoundError(f"unknown topic {topic!r}")
        return self._topics[topic]

    def _partition(self, topic: str, partition: int) -> Partition:
        logs = self._logs(topic)
        if not 0 <= partition < len(logs):
            raise ValidationError(f"topic {topic!r} has no partition {partition}")
        return logs[partition]

    def partition_count(self, topic: str) -> int:
        with self._lock:
            return len(self._logs(topic))

    def append(
        self,
        topic: str,
        partition: int,
        key: str | None,
        value: str | None,
        *,
        producer_id: str | None = None,
        sequence: int | None = None,
    ) -> Record:
        """Append one record. With a producer id, deduplicate by sequence number: a repeated
        sequence is acknowledged with the original record, a gap is rejected (Kafka's
        ``OutOfOrderSequenceException``), so the log gets neither duplicates nor holes."""
        with self._lock:
            log = self._partition(topic, partition)
            if producer_id is None:
                return log.append(key, value, self._clock.now())
            if sequence is None:
                raise ValidationError("an idempotent append needs a sequence number")
            dedup_key = (producer_id, topic, partition)
            last = self._sequences.get(dedup_key)
            if last is not None and sequence == last[0]:
                return last[1]  # the retry of a send whose ack was lost
            expected = 0 if last is None else last[0] + 1
            if sequence != expected:
                raise ConflictError(f"sequence {sequence} out of order, expected {expected}")
            record = log.append(key, value, self._clock.now())
            self._sequences[dedup_key] = (sequence, record)
            return record

    def fetch(self, topic: str, partition: int, offset: int, limit: int = 100) -> list[Record]:
        if limit <= 0:
            raise ValidationError("limit must be positive")
        with self._lock:
            return self._partition(topic, partition).read(offset, limit)

    def offsets(self, topic: str, partition: int) -> tuple[int, int]:  # (log start, end)
        with self._lock:
            log = self._partition(topic, partition)
            return log.log_start_offset, log.end_offset

    def commit(self, group: str, topic: str, partition: int, offset: int) -> None:
        """Record that ``group`` has processed everything below ``offset`` on this partition."""
        with self._lock:
            log = self._partition(topic, partition)
            if not 0 <= offset <= log.end_offset:
                raise ValidationError(f"offset {offset} is outside [0, {log.end_offset}]")
            self._committed[(group, topic, partition)] = offset

    def committed(self, group: str, topic: str, partition: int) -> int | None:
        with self._lock:
            return self._committed.get((group, topic, partition))

    def lag(self, group: str, topic: str) -> dict[int, int]:
        """Per partition: end offset minus the group's committed offset."""
        with self._lock:
            lag: dict[int, int] = {}
            for log in self._logs(topic):
                done = self._committed.get((group, topic, log.index), log.log_start_offset)
                lag[log.index] = log.end_offset - done
            return lag

    def compact(self, topic: str) -> int:
        with self._lock:
            return sum(log.compact() for log in self._logs(topic))

    def expire(self, topic: str) -> int:
        """Apply the topic's time retention: drop records older than ``now - retention``."""
        with self._lock:
            logs, retention = self._logs(topic), self._retention[topic]
            if retention is None:
                return 0
            before = self._clock.now() - retention
            return sum(log.expire(before) for log in logs)


# --8<-- [end:broker]
# --8<-- [start:producer]
class Producer:
    """A client that partitions records and numbers them for broker-side deduplication.
    ``_lock`` guards the per-partition sequence counters and the round-robin counter."""

    def __init__(self, broker: Broker, producer_id: str) -> None:
        self._broker = broker
        self._producer_id = producer_id
        self._sequences: dict[tuple[str, int], int] = {}
        self._round_robin = itertools.count()
        self._lock = threading.Lock()

    def partition_for(self, topic: str, key: str | None) -> int:
        count = self._broker.partition_count(topic)
        if key is None:
            with self._lock:
                return next(self._round_robin) % count
        return partition_hash(key) % count

    def send(
        self, topic: str, value: str | None, key: str | None = None, attempts: int = 1
    ) -> Record:
        """Send one record. ``attempts > 1`` repeats the identical send, as a producer whose
        acks were lost would, so the caller can watch the broker drop the duplicates."""
        if attempts <= 0:
            raise ValidationError("attempts must be positive")
        partition = self.partition_for(topic, key)
        with self._lock:  # one in-flight send per partition keeps the sequence gap-free
            sequence = self._sequences.get((topic, partition), 0)
            records = [
                self._broker.append(
                    topic, partition, key, value, producer_id=self._producer_id, sequence=sequence
                )
                for _ in range(attempts)
            ]
            self._sequences[(topic, partition)] = sequence + 1
            return records[0]


# --8<-- [end:producer]
# --8<-- [start:consumer]
class ConsumerGroup:
    """Range assignment with an eager (stop-the-world) rebalance. ``_lock`` guards the members,
    the assignment and ``_positions`` (the next offset each member reads per partition);
    ``commit`` copies positions to the broker. A membership change revokes every partition and
    restarts it from its *committed* offset, so polled-but-uncommitted records are redelivered."""

    def __init__(
        self, broker: Broker, group_id: str, topic: str, auto_offset_reset: str = "earliest"
    ) -> None:
        if auto_offset_reset not in ("earliest", "latest"):
            raise ValidationError("auto_offset_reset must be 'earliest' or 'latest'")
        self._broker = broker
        self.group_id = group_id
        self.topic = topic
        self._reset = auto_offset_reset
        self._members: list[str] = []
        self._assignment: dict[str, list[int]] = {}
        self._positions: dict[tuple[str, int], int] = {}
        self.generation = 0
        self._lock = threading.Lock()

    def _rebalance(self) -> None:
        count, members = self._broker.partition_count(self.topic), sorted(self._members)
        base, extra = divmod(count, len(members) or 1)
        sizes = [base + (1 if i < extra else 0) for i in range(len(members))]  # range assignor
        bounds = list(itertools.accumulate([0, *sizes]))
        self._assignment = {m: list(range(bounds[i], bounds[i + 1])) for i, m in enumerate(members)}
        self._positions.clear()
        self.generation += 1

    def _require(self, member: str) -> list[int]:
        if member not in self._assignment:
            raise NotFoundError(f"{member!r} is not in group {self.group_id!r}")
        return self._assignment[member]

    def join(self, member: str) -> list[int]:
        with self._lock:
            if member in self._members:
                raise ConflictError(f"{member!r} is already in group {self.group_id!r}")
            self._members.append(member)
            self._rebalance()
            return list(self._assignment[member])

    def leave(self, member: str) -> None:
        """A clean leave or a missed heartbeat: the coordinator rebalances either way."""
        with self._lock:
            self._require(member)
            self._members.remove(member)
            self._rebalance()

    def assignment(self, member: str) -> list[int]:
        with self._lock:
            return list(self._require(member))

    def _start_offset(self, partition: int) -> int:
        committed = self._broker.committed(self.group_id, self.topic, partition)
        if committed is not None:
            return committed
        log_start, end = self._broker.offsets(self.topic, partition)
        return log_start if self._reset == "earliest" else end

    def poll(self, member: str, max_records: int = 100) -> list[Record]:
        """Read from each assigned partition in turn; only the in-memory position advances."""
        with self._lock:
            out: list[Record] = []
            for partition in self._require(member):
                if len(out) >= max_records:
                    break
                position = self._positions.get((member, partition))
                if position is None:
                    position = self._start_offset(partition)
                limit = max_records - len(out)
                try:
                    records = self._broker.fetch(self.topic, partition, position, limit)
                except OffsetOutOfRangeError:  # retention overtook us: apply the reset policy
                    position = self._start_offset(partition)
                    records = self._broker.fetch(self.topic, partition, position, limit)
                if records:
                    position = records[-1].offset + 1
                self._positions[(member, partition)] = position
                out.extend(records)
            return out

    def commit(self, member: str) -> dict[int, int]:
        """Commit the member's positions; returns ``{partition: committed offset}``."""
        with self._lock:
            committed: dict[int, int] = {}
            for partition in self._require(member):
                position = self._positions.get((member, partition))
                if position is not None:
                    self._broker.commit(self.group_id, self.topic, partition, position)
                    committed[partition] = position
            return committed

    def lag(self) -> dict[int, int]:
        return self._broker.lag(self.group_id, self.topic)


# --8<-- [end:consumer]



def main() -> None:
    def values(records: list[Record]) -> str:
        return " ".join(r.value or "" for r in records)

    clock = FakeClock(start=1_000.0)
    broker = Broker(clock=clock)
    broker.create_topic("orders", partitions=3, retention=3_600)
    producer = Producer(broker, producer_id="checkout-1")
    for i, user in enumerate(["ann", "bob", "fay", "ann", "bob", "ann", "dan", "fay", "ann"]):
        producer.send("orders", value=f"order-{i}", key=user)
    for p in range(3):
        print(f"p{p}: " + " ".join(f"{r.key}@{r.offset}" for r in broker.fetch("orders", p, 0)))
    r = producer.send("orders", "order-9", key="bob", attempts=3)
    total = sum(broker.offsets("orders", p)[1] for p in range(3))
    print(f"idempotent send, 3 attempts: one record p{r.partition}@{r.offset}, log 9 -> {total}")

    group = ConsumerGroup(broker, "billing", "orders")
    group.join("c1")
    group.join("c2")
    print(f"generation {group.generation}: c1={group.assignment('c1')} c2={group.assignment('c2')}")
    print(f"c1 polls {len(group.poll('c1'))}, commits {group.commit('c1')}")
    lost = group.poll("c2")
    print(f"c2 polls {len(lost)} ({values(lost)}) and crashes before committing")
    group.leave("c2")
    again, c1 = group.poll("c1"), group.assignment("c1")
    print(f"generation {group.generation}: c1={c1} polls {len(again)} again: {values(again)}")
    lag_before = group.lag()
    group.commit("c1")
    print(f"lag before commit {lag_before}, after commit {group.lag()}")

    broker.create_topic("balances", partitions=1)
    for i, user in enumerate(["ann", "bob", "ann", "ann", "bob", "cid"]):
        producer.send("balances", value=str(100 + i), key=user)
    removed = broker.compact("balances")
    kept = " ".join(f"{r.key}={r.value}@{r.offset}" for r in broker.fetch("balances", 0, 0))
    print(f"compact balances: removed {removed}, kept {kept}")

    clock.advance(7_200)
    producer.send("orders", "order-10", key="dan")
    expired = broker.expire("orders")
    starts = [broker.offsets("orders", p)[0] for p in range(3)]
    audit = ConsumerGroup(broker, "audit", "orders")
    audit.join("a")
    print(f"retention: expired {expired}, log start {starts}, new group reads {values(audit.poll('a'))}")


if __name__ == "__main__":
    main()
