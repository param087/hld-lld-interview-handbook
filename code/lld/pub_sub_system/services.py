"""Delivery workers, consumer groups and the broker that mediates between them."""

from __future__ import annotations

import threading
from typing import ClassVar

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.pub_sub_system.models import (
    BrokerClosedError,
    BrokerState,
    Consumer,
    DeadLetter,
    DeliveryState,
    Message,
    OffsetOutOfRangeError,
    Partitioner,
    Record,
    RetentionPolicy,
    RetryPolicy,
    SubscriptionError,
    TopicExistsError,
    TopicNotFoundError,
)
from lld.pub_sub_system.storage import DeadLetterQueue, OffsetStore, Partition, Topic

FETCH_TIMEOUT = 0.02  # how long a parked worker waits before re-checking its stop flag


# --8<-- [start:worker]
class DeliveryWorker:
    """One thread per ``(group, partition)``. That pairing *is* the ordering guarantee.

    Because exactly one worker owns a partition inside a group, records on that
    partition are delivered in offset order and no two consumers in the group
    ever see the same record. Ordering across partitions is not promised -- say
    that out loud before the interviewer asks.
    """

    def __init__(
        self,
        group: str,
        partition: Partition,
        consumer: Consumer,
        offsets: OffsetStore,
        dlq: DeadLetterQueue,
        retry: RetryPolicy,
        clock: Clock,
        on_progress: Broker | None = None,
    ) -> None:
        self.group = group
        self.partition = partition
        self.consumer = consumer
        self.state = DeliveryState.PENDING
        self.delivered = 0
        self.retried = 0
        self.dead_lettered = 0
        self.skipped = 0  # records lost to retention before this group reached them
        self._offsets = offsets
        self._dlq = dlq
        self._retry = retry
        self._clock = clock
        self._broker = on_progress
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> DeliveryWorker:
        self._thread = threading.Thread(
            target=self._run, name=f"{self.group}-{self.partition.topic}-{self.partition.index}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.partition.wake()  # unpark immediately instead of waiting out FETCH_TIMEOUT
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self) -> None:
        topic, index = self.partition.topic, self.partition.index
        while not self._stop.is_set():
            # Re-read the offset every turn: a replay may have moved it under us.
            offset = self._offsets.committed(self.group, topic, index)
            try:
                record = self.partition.fetch(offset, FETCH_TIMEOUT, self._stop)
            except OffsetOutOfRangeError:
                earliest = self.partition.earliest_offset()
                self.skipped += earliest - offset
                self._offsets.seek(self.group, topic, index, earliest)
                continue
            if record is None:
                continue
            acked = self._deliver(record)
            if self._stop.is_set() and not acked:
                return  # at-least-once: no commit, so the record is redelivered on restart
            self._offsets.advance(self.group, topic, index, expected=offset, new=offset + 1)
            if self._broker is not None:
                self._broker.report_progress(self.group, topic, index, offset + 1)

    def _deliver(self, record: Record) -> bool:
        """Retry with exponential backoff, then dead-letter so the partition keeps moving."""
        for attempt in range(1, self._retry.max_attempts + 1):
            self.state = DeliveryState.IN_FLIGHT
            try:
                self.consumer.on_message(record)
            except Exception as exc:  # a consumer bug must not kill the worker
                if not self._retry.should_retry(attempt):
                    self.state = DeliveryState.DEAD_LETTERED
                    self.dead_lettered += 1
                    self._dlq.add(
                        DeadLetter(
                            record=record,
                            group=self.group,
                            attempts=attempt,
                            error=f"{type(exc).__name__}: {exc}",
                            failed_at=self._clock.now(),
                        )
                    )
                    return False
                self.state = DeliveryState.RETRY_SCHEDULED
                self.retried += 1
                if self._stop.wait(self._retry.delay_for(attempt)):
                    return False  # shutting down mid-backoff: leave the offset uncommitted
            else:
                self.state = DeliveryState.ACKED
                self.delivered += 1
                return True
        return False


# --8<-- [end:worker]


# --8<-- [start:group]
class ConsumerGroup:
    """A set of consumers that share one cursor per partition.

    Adding a consumer rebalances: the workers stop, partitions are dealt out
    again, and new workers start from the *stored* offsets. Nothing is lost,
    because progress lives in the OffsetStore and never in a thread.
    """

    def __init__(
        self,
        name: str,
        topic: Topic,
        offsets: OffsetStore,
        dlq: DeadLetterQueue,
        retry: RetryPolicy,
        clock: Clock,
        broker: Broker,
    ) -> None:
        self.name = name
        self.topic = topic
        self._offsets = offsets
        self._dlq = dlq
        self._retry = retry
        self._clock = clock
        self._broker = broker
        self._lock = threading.Lock()  # serialises rebalances
        self._consumers: list[Consumer] = []
        self._workers: list[DeliveryWorker] = []
        self._totals = {"delivered": 0, "retried": 0, "dead_lettered": 0, "skipped": 0}

    def add_consumer(self, consumer: Consumer) -> None:
        with self._lock:
            if any(c.name == consumer.name for c in self._consumers):
                raise SubscriptionError(f"consumer {consumer.name!r} is already in group {self.name!r}")
            self._consumers.append(consumer)
            self._rebalance_locked()

    def assignment(self) -> dict[str, list[int]]:
        with self._lock:
            plan: dict[str, list[int]] = {c.name: [] for c in self._consumers}
            for worker in self._workers:
                plan[worker.consumer.name].append(worker.partition.index)
            return plan

    def pause(self) -> None:
        with self._lock:
            self._stop_workers_locked()

    def resume(self) -> None:
        with self._lock:
            self._rebalance_locked()

    def close(self) -> None:
        with self._lock:
            self._stop_workers_locked()

    def stats(self) -> dict[str, int]:
        """Counters survive rebalances: retired workers fold their totals into the group."""
        with self._lock:
            return {name: total + self._live_locked(name) for name, total in self._totals.items()}

    def _live_locked(self, name: str) -> int:
        return sum(getattr(worker, name) for worker in self._workers)

    def _stop_workers_locked(self) -> None:
        for worker in self._workers:
            worker.stop()
            for name in self._totals:
                self._totals[name] += getattr(worker, name)
        self._workers = []

    def _rebalance_locked(self) -> None:
        self._stop_workers_locked()
        if not self._consumers:
            return
        for partition in self.topic.partitions():
            consumer = self._consumers[partition.index % len(self._consumers)]
            worker = DeliveryWorker(
                self.name, partition, consumer, self._offsets, self._dlq, self._retry, self._clock, self._broker
            )
            self._workers.append(worker.start())


# --8<-- [end:group]


# --8<-- [start:broker]
class Broker:
    """Mediator: producers and consumers know the broker, never each other.

    One instance is enough for a process, and ``instance()`` provides it, but the
    constructor stays public so a test owns its own broker and its own threads.
    """

    _instance_lock: ClassVar[threading.Lock] = threading.Lock()
    _instance: ClassVar[Broker | None] = None

    def __init__(
        self,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        retry: RetryPolicy | None = None,
        partitioner: Partitioner | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("m")
        self._retry = retry or RetryPolicy()
        self._partitioner = partitioner
        self._lock = threading.RLock()  # guards the topic and group registries
        self._progress = threading.Condition()  # signalled after every committed offset
        self._topics: dict[str, Topic] = {}
        self._groups: dict[tuple[str, str], ConsumerGroup] = {}
        self.offsets = OffsetStore()
        self.dlq = DeadLetterQueue()
        self.state = BrokerState.RUNNING

    @classmethod
    def instance(cls) -> Broker:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._instance_lock:
            cls._instance = None

    def create_topic(self, name: str, partitions: int = 1, retention: RetentionPolicy | None = None) -> Topic:
        with self._lock:
            self._require_running()
            if name in self._topics:
                raise TopicExistsError(f"topic {name!r} already exists")
            topic = Topic(name, partitions, retention, self._clock, self._partitioner)
            self._topics[name] = topic
            return topic

    def topic(self, name: str) -> Topic:
        with self._lock:
            try:
                return self._topics[name]
            except KeyError:
                raise TopicNotFoundError(f"unknown topic {name!r}") from None

    def publish(self, topic: str, payload: str, key: str | None = None, headers: dict[str, str] | None = None) -> Record:
        """Route by key, append under the partition lock, and wake the parked workers."""
        target = self.topic(topic)
        self._require_running()
        message = Message(
            id=self._ids.next_id(),
            topic=topic,
            payload=payload,
            key=key,
            headers=dict(headers or {}),
            created=self._clock.now(),
        )
        return target.route(key).append(message)  # may block: that is backpressure

    def subscribe(self, topic: str, group: str, consumer: Consumer) -> ConsumerGroup:
        target = self.topic(topic)
        if not group:
            raise SubscriptionError("group name must be non-empty")
        with self._lock:
            self._require_running()
            key = (group, topic)
            if key not in self._groups:
                self._groups[key] = ConsumerGroup(
                    group, target, self.offsets, self.dlq, self._retry, self._clock, self
                )
            consumer_group = self._groups[key]
        consumer_group.add_consumer(consumer)  # outside the registry lock: it starts threads
        return consumer_group

    def group_names(self, topic: str) -> list[str]:
        with self._lock:
            return [group for (group, subscribed) in self._groups if subscribed == topic]

    def group(self, name: str, topic: str) -> ConsumerGroup:
        with self._lock:
            try:
                return self._groups[(name, topic)]
            except KeyError:
                raise SubscriptionError(f"group {name!r} is not subscribed to {topic!r}") from None

    def report_progress(self, group: str, topic: str, partition: int, offset: int) -> None:
        """A worker committed. Reclaim space in the partition and wake anyone draining."""
        low = self.offsets.low_water(topic, partition, self.group_names(topic))
        self.topic(topic).partition(partition).set_low_water(low)
        with self._progress:
            self._progress.notify_all()

    def lag(self, group: str, topic: str) -> int:
        target = self.topic(topic)
        return sum(
            p.next_offset() - self.offsets.committed(group, topic, p.index) for p in target.partitions()
        )

    def drain(self, timeout: float = 2.0) -> bool:
        """Barrier for tests and shutdown: return once every group has caught up."""
        with self._progress:
            return self._progress.wait_for(self._all_caught_up, timeout=timeout)

    def _all_caught_up(self) -> bool:
        with self._lock:
            pairs = list(self._groups)
        return all(self.lag(group, topic) == 0 for group, topic in pairs)

    def replay(self, group: str, topic: str, from_offset: int = 0) -> None:
        """Pause the group, rewind every partition, resume. Offsets are just numbers."""
        with self._lock:
            consumer_group = self._groups.get((group, topic))
        if consumer_group is None:
            raise SubscriptionError(f"group {group!r} is not subscribed to {topic!r}")
        consumer_group.pause()
        target = self.topic(topic)
        for partition in target.partitions():
            self.offsets.seek(group, topic, partition.index, max(from_offset, partition.earliest_offset()))
        consumer_group.resume()

    def close(self, timeout: float = 2.0) -> None:
        """Graceful shutdown: stop accepting work, let the workers finish, then stop them."""
        with self._lock:
            if self.state is not BrokerState.RUNNING:
                return
            self.state = BrokerState.DRAINING
            groups, topics = list(self._groups.values()), list(self._topics.values())
        self.drain(timeout)
        for consumer_group in groups:
            consumer_group.close()
        for topic in topics:
            topic.close()
        with self._lock:
            self.state = BrokerState.STOPPED

    def _require_running(self) -> None:
        if self.state is not BrokerState.RUNNING:
            raise BrokerClosedError(f"broker is {self.state}")


# --8<-- [end:broker]
