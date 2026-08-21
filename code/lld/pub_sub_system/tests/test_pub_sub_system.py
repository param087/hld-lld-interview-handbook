import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, SequentialIdGenerator
from lld.pub_sub_system.consumers import ConsumerError, FlakyConsumer, RecordingConsumer
from lld.pub_sub_system.models import (
    BackpressureError,
    BrokerClosedError,
    BrokerState,
    FullPolicy,
    RetentionPolicy,
    RetryPolicy,
    SubscriptionError,
    TopicExistsError,
    TopicNotFoundError,
)
from lld.pub_sub_system.services import Broker
from lld.pub_sub_system.strategies import KeyHashPartitioner, RoundRobinPartitioner

ORDERS = "orders"
FAST_RETRY = RetryPolicy(max_attempts=3, base_delay=0.001, max_delay=0.004)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_700_000_000)


@pytest.fixture
def broker(clock: FakeClock):
    instance = Broker(clock=clock, ids=SequentialIdGenerator("m"), retry=FAST_RETRY)
    yield instance
    instance.close(timeout=1.0)


# --8<-- [start:groups]
def test_two_groups_each_get_every_record_on_their_own_cursor(broker: Broker) -> None:
    broker.create_topic(ORDERS, partitions=2)
    billing, audit = RecordingConsumer("billing-1"), RecordingConsumer("audit-1")
    broker.subscribe(ORDERS, "billing", billing)
    broker.subscribe(ORDERS, "audit", audit)

    for key, event in [("alice", "created"), ("bob", "created"), ("alice", "paid")]:
        broker.publish(ORDERS, f"{key}:{event}", key=key)
    assert broker.drain(timeout=1.0)

    assert sorted(billing.payloads()) == sorted(audit.payloads())
    assert len(billing.payloads()) == 3
    assert billing.keys_in_order("alice") == ["alice:created", "alice:paid"]  # per-key order
    assert broker.lag("billing", ORDERS) == 0


# --8<-- [end:groups]


def test_a_key_always_lands_on_the_same_partition(broker: Broker) -> None:
    topic = broker.create_topic(ORDERS, partitions=4)
    chosen = {topic.route("alice").index for _ in range(50)}
    assert len(chosen) == 1  # crc32, not the salted built-in hash
    assert {topic.route(f"user-{i}").index for i in range(40)} == {0, 1, 2, 3}


@pytest.mark.parametrize(
    ("action", "error"),
    [
        (lambda b: b.publish("nope", "x"), TopicNotFoundError),
        (lambda b: b.create_topic(ORDERS), TopicExistsError),
        (lambda b: b.subscribe(ORDERS, "", RecordingConsumer("c")), SubscriptionError),
        (lambda b: b.replay("ghost", ORDERS), SubscriptionError),
    ],
)
def test_invalid_requests_are_rejected(broker: Broker, action, error) -> None:
    broker.create_topic(ORDERS, partitions=1)
    with pytest.raises(error):
        action(broker)


def test_a_consumer_cannot_join_the_same_group_twice(broker: Broker) -> None:
    broker.create_topic(ORDERS, partitions=1)
    consumer = RecordingConsumer("worker-1")
    broker.subscribe(ORDERS, "billing", consumer)
    with pytest.raises(SubscriptionError):
        broker.subscribe(ORDERS, "billing", consumer)


# --8<-- [start:retry]
def test_a_transient_failure_is_retried_and_a_poison_record_is_dead_lettered(broker: Broker) -> None:
    broker.create_topic(ORDERS, partitions=1)
    consumer = FlakyConsumer("shipping-1", fail_times=1, poison="bob:cancelled")
    group = broker.subscribe(ORDERS, "shipping", consumer)

    broker.publish(ORDERS, "alice:paid", key="alice")
    broker.publish(ORDERS, "bob:cancelled", key="bob")
    broker.publish(ORDERS, "alice:shipped", key="alice")
    assert broker.drain(timeout=1.0)

    assert consumer.payloads() == ["alice:paid", "alice:shipped"]  # the poison never acked
    assert consumer.attempts("alice:paid") == 2  # one failure, then success
    [letter] = broker.dlq.letters("shipping")
    assert letter.attempts == 3 and letter.record.payload == "bob:cancelled"
    assert group.stats()["dead_lettered"] == 1
    assert broker.lag("shipping", ORDERS) == 0  # the DLQ let the partition move on


# --8<-- [end:retry]


def test_delivery_state_walks_pending_in_flight_acked(broker: Broker) -> None:
    broker.create_topic(ORDERS, partitions=1)
    seen: list[str] = []

    class Watcher:
        name = "watcher-1"

        def on_message(self, record) -> None:
            seen.append(str(record))

    broker.subscribe(ORDERS, "watch", Watcher())
    broker.publish(ORDERS, "hello", key="alice")
    assert broker.drain(timeout=1.0)
    assert seen == ["orders/0@0 hello"]
    assert broker.offsets.committed("watch", ORDERS, 0) == 1


def test_replay_rewinds_the_group_and_redelivers_everything(broker: Broker) -> None:
    broker.create_topic(ORDERS, partitions=2)
    consumer = RecordingConsumer("audit-1")
    broker.subscribe(ORDERS, "audit", consumer)
    for i in range(6):
        broker.publish(ORDERS, f"event-{i}", key=f"user-{i}")
    assert consumer.wait_for(6, timeout=1.0)

    broker.replay("audit", ORDERS, from_offset=0)
    assert consumer.wait_for(12, timeout=1.0)
    assert sorted(consumer.payloads())[:2] == ["event-0", "event-0"]  # each seen twice


def test_a_late_subscriber_reads_the_backlog_from_offset_zero(broker: Broker) -> None:
    broker.create_topic(ORDERS, partitions=1)
    for i in range(4):
        broker.publish(ORDERS, f"event-{i}", key="alice")

    latecomer = RecordingConsumer("analytics-1")
    broker.subscribe(ORDERS, "analytics", latecomer)
    assert latecomer.wait_for(4, timeout=1.0)
    assert latecomer.payloads() == ["event-0", "event-1", "event-2", "event-3"]


# --8<-- [start:backpressure]
def test_a_full_partition_blocks_the_producer_then_gives_up(broker: Broker) -> None:
    retention = RetentionPolicy(max_messages=2, on_full=FullPolicy.BLOCK, block_timeout=0.02)
    broker.create_topic(ORDERS, partitions=1, retention=retention)
    broker.publish(ORDERS, "first", key="alice")
    broker.publish(ORDERS, "second", key="alice")

    with pytest.raises(BackpressureError):  # nobody is consuming, so nothing can be reclaimed
        broker.publish(ORDERS, "third", key="alice")

    drain_me = RecordingConsumer("billing-1")
    broker.subscribe(ORDERS, "billing", drain_me)
    assert drain_me.wait_for(2, timeout=1.0)
    assert broker.drain(timeout=1.0)
    broker.publish(ORDERS, "third", key="alice")  # the acked records were reclaimed
    assert drain_me.wait_for(3, timeout=1.0)


# --8<-- [end:backpressure]


def test_drop_oldest_sheds_instead_of_blocking(broker: Broker) -> None:
    retention = RetentionPolicy(max_messages=2, on_full=FullPolicy.DROP_OLDEST)
    topic = broker.create_topic(ORDERS, partitions=1, retention=retention)
    for i in range(5):
        broker.publish(ORDERS, f"event-{i}", key="alice")

    partition = topic.partition(0)
    assert partition.size() == 2 and partition.dropped == 3
    assert partition.earliest_offset() == 3 and partition.next_offset() == 5

    consumer = RecordingConsumer("billing-1")
    broker.subscribe(ORDERS, "billing", consumer)
    assert consumer.wait_for(2, timeout=1.0)
    assert consumer.payloads() == ["event-3", "event-4"]  # the slow group was fast-forwarded
    assert broker.group("billing", ORDERS).stats()["skipped"] == 3


def test_age_based_retention_trims_records_using_the_injected_clock(clock: FakeClock, broker: Broker) -> None:
    retention = RetentionPolicy(max_messages=64, max_age_seconds=60)
    topic = broker.create_topic(ORDERS, partitions=1, retention=retention)
    broker.publish(ORDERS, "stale", key="alice")
    clock.advance(61)
    broker.publish(ORDERS, "fresh", key="alice")

    partition = topic.partition(0)
    assert partition.size() == 1 and partition.dropped == 1 and partition.earliest_offset() == 1


# --8<-- [start:concurrency]
def test_concurrent_producers_lose_no_record_and_assign_unique_offsets(broker: Broker) -> None:
    topic = broker.create_topic(ORDERS, partitions=4, retention=RetentionPolicy(max_messages=512))
    consumers = [RecordingConsumer(f"billing-{i}") for i in range(2)]
    for consumer in consumers:
        broker.subscribe(ORDERS, "billing", consumer)

    def publish(i: int) -> tuple[int, int]:
        record = broker.publish(ORDERS, f"event-{i:03d}", key=f"user-{i}")
        return record.partition, record.offset

    with ThreadPoolExecutor(max_workers=8) as pool:
        placed = list(pool.map(publish, range(400)))

    assert len(set(placed)) == 400  # every (partition, offset) pair is unique
    assert sum(p.next_offset() for p in topic.partitions()) == 400
    assert broker.drain(timeout=2.0)
    delivered = [payload for c in consumers for payload in c.payloads()]
    assert sorted(delivered) == [f"event-{i:03d}" for i in range(400)]  # each exactly once


# --8<-- [end:concurrency]


def test_adding_a_consumer_rebalances_without_losing_the_cursor(broker: Broker) -> None:
    broker.create_topic(ORDERS, partitions=2)
    first = RecordingConsumer("worker-1")
    group = broker.subscribe(ORDERS, "billing", first)
    broker.publish(ORDERS, "one", key="alice")
    broker.publish(ORDERS, "two", key="bob")
    assert first.wait_for(2, timeout=1.0)
    assert group.assignment() == {"worker-1": [0, 1]}

    second = RecordingConsumer("worker-2")
    broker.subscribe(ORDERS, "billing", second)
    assert group.assignment() == {"worker-1": [0], "worker-2": [1]}

    broker.publish(ORDERS, "three", key="alice")
    assert broker.drain(timeout=1.0)
    everything = first.payloads() + second.payloads()
    assert sorted(everything) == ["one", "three", "two"]  # nothing redelivered, nothing lost


def test_graceful_shutdown_drains_then_refuses_new_work(broker: Broker) -> None:
    broker.create_topic(ORDERS, partitions=2)
    consumer = RecordingConsumer("billing-1")
    broker.subscribe(ORDERS, "billing", consumer)
    for i in range(20):
        broker.publish(ORDERS, f"event-{i}", key=f"user-{i}")

    broker.close(timeout=2.0)
    assert broker.state is BrokerState.STOPPED
    assert len(consumer.payloads()) == 20 and broker.lag("billing", ORDERS) == 0
    with pytest.raises(BrokerClosedError):
        broker.publish(ORDERS, "too late", key="alice")


def test_offset_store_compare_and_set_protects_a_concurrent_seek() -> None:
    broker = Broker(clock=FakeClock(start=0), ids=SequentialIdGenerator("m"))
    store = broker.offsets
    store.seek("billing", ORDERS, 0, 5)
    assert store.advance("billing", ORDERS, 0, expected=5, new=6) is True
    assert store.advance("billing", ORDERS, 0, expected=5, new=6) is False  # a seek moved it
    assert store.committed("billing", ORDERS, 0) == 6


def test_a_raising_consumer_never_kills_the_worker(broker: Broker) -> None:
    broker.create_topic(ORDERS, partitions=1)
    barrier = threading.Event()

    class Grumpy:
        name = "grumpy-1"
        seen = 0

        def on_message(self, record) -> None:
            Grumpy.seen += 1
            if record.payload == "boom":
                raise ConsumerError("no")
            barrier.set()

    broker.subscribe(ORDERS, "grumpy", Grumpy())
    broker.publish(ORDERS, "boom", key="alice")
    broker.publish(ORDERS, "fine", key="alice")
    assert barrier.wait(timeout=1.0)  # the worker survived three failed attempts
    assert len(broker.dlq) == 1


@pytest.mark.parametrize(
    ("partitioner", "expected"),
    [(RoundRobinPartitioner(), [0, 1, 2, 0]), (KeyHashPartitioner(), [0, 1, 2, 0])],
)
def test_keyless_messages_are_spread_round_robin(partitioner, expected: list[int]) -> None:
    assert [partitioner.partition_for(None, 3) for _ in range(4)] == expected
