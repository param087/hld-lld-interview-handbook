from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, FakeClock, NotFoundError, ValidationError
from hld.mini_kafka import (
    Broker,
    ConsumerGroup,
    OffsetOutOfRangeError,
    Producer,
    partition_hash,
)


def make_broker(partitions: int = 3, retention: float | None = None) -> tuple[Broker, FakeClock]:
    clock = FakeClock(start=1_000.0)
    broker = Broker(clock=clock)
    broker.create_topic("orders", partitions=partitions, retention=retention)
    return broker, clock


def values(records: list) -> list[str]:
    return [r.value for r in records]


def test_same_key_lands_on_one_partition_in_send_order() -> None:
    broker, _ = make_broker(partitions=4)
    producer = Producer(broker, "p1")
    keys = [f"user:{i}" for i in range(50)]
    for round_no in range(5):
        for key in keys:
            producer.send("orders", value=f"{key}#{round_no}", key=key)
    per_key: dict[str, list[str]] = {}
    owners: dict[str, set[int]] = {}
    for p in range(4):
        for record in broker.fetch("orders", p, 0, limit=1_000):
            assert record.key is not None
            per_key.setdefault(record.key, []).append(record.value or "")
            owners.setdefault(record.key, set()).add(p)
    assert all(len(ps) == 1 for ps in owners.values())  # a key never spans partitions
    assert all(vals == [f"{k}#{i}" for i in range(5)] for k, vals in per_key.items())
    assert len({next(iter(ps)) for ps in owners.values()}) == 4  # keys spread over all of them
    assert producer.partition_for("orders", "user:7") == partition_hash("user:7") % 4


def test_unkeyed_records_round_robin_across_partitions() -> None:
    broker, _ = make_broker(partitions=3)
    producer = Producer(broker, "p1")
    partitions = [producer.send("orders", value=str(i)).partition for i in range(6)]
    assert partitions == [0, 1, 2, 0, 1, 2]


def test_idempotent_producer_drops_duplicates_and_rejects_gaps() -> None:
    broker, _ = make_broker(partitions=1)
    producer = Producer(broker, "checkout")
    first = producer.send("orders", value="a", key="k", attempts=3)
    second = producer.send("orders", value="b", key="k")
    assert (first.offset, second.offset) == (0, 1)
    assert values(broker.fetch("orders", 0, 0)) == ["a", "b"]
    # a retry of sequence 1 is acknowledged with the original record, nothing is appended
    again = broker.append("orders", 0, "k", "b", producer_id="checkout", sequence=1)
    assert again == second
    assert broker.offsets("orders", 0) == (0, 2)
    with pytest.raises(ConflictError):
        broker.append("orders", 0, "k", "c", producer_id="checkout", sequence=5)
    with pytest.raises(ValidationError):
        broker.append("orders", 0, "k", "c", producer_id="checkout")
    # a plain (non-idempotent) append has no protection: the duplicate lands
    broker.append("orders", 0, "k", "dup")
    broker.append("orders", 0, "k", "dup")
    assert values(broker.fetch("orders", 0, 2)) == ["dup", "dup"]


def test_range_assignor_and_generation_bumps() -> None:
    broker, _ = make_broker(partitions=5)
    group = ConsumerGroup(broker, "g", "orders")
    assert group.join("c1") == [0, 1, 2, 3, 4]
    assert group.join("c2") == [3, 4]
    assert group.assignment("c1") == [0, 1, 2]
    group.join("c3")
    assert [group.assignment(m) for m in ("c1", "c2", "c3")] == [[0, 1], [2, 3], [4]]
    assert group.generation == 3
    group.leave("c1")
    assert [group.assignment(m) for m in ("c2", "c3")] == [[0, 1, 2], [3, 4]]
    assert group.generation == 4
    with pytest.raises(NotFoundError):
        group.assignment("c1")
    with pytest.raises(ConflictError):
        group.join("c2")


def test_rebalance_redelivers_uncommitted_records_only() -> None:
    broker, _ = make_broker(partitions=2)
    producer = Producer(broker, "p1")
    for i in range(12):
        producer.send("orders", value=f"v{i}", key=f"k{i}")
    by_partition = {p: values(broker.fetch("orders", p, 0)) for p in range(2)}
    assert all(by_partition.values())
    group = ConsumerGroup(broker, "g", "orders")
    group.join("c1")
    group.join("c2")
    assert (group.assignment("c1"), group.assignment("c2")) == ([0], [1])
    assert values(group.poll("c1")) == by_partition[0]
    assert group.commit("c1") == {0: len(by_partition[0])}
    assert values(group.poll("c2")) == by_partition[1]  # polled, never committed
    group.leave("c2")
    assert group.assignment("c1") == [0, 1]
    redelivered = values(group.poll("c1"))
    assert redelivered == by_partition[1]  # partition 0 was committed, so it is not replayed
    assert group.poll("c1") == []
    assert group.lag() == {0: 0, 1: len(by_partition[1])}
    group.commit("c1")
    assert group.lag() == {0: 0, 1: 0}


def test_commit_validates_offsets_and_lag_counts_uncommitted_records() -> None:
    broker, _ = make_broker(partitions=1)
    producer = Producer(broker, "p1")
    for i in range(4):
        producer.send("orders", value=str(i), key="k")
    assert broker.lag("g", "orders") == {0: 4}
    broker.commit("g", "orders", 0, 3)
    assert broker.committed("g", "orders", 0) == 3
    assert broker.lag("g", "orders") == {0: 1}
    with pytest.raises(ValidationError):
        broker.commit("g", "orders", 0, 5)
    with pytest.raises(ValidationError):
        broker.commit("g", "orders", 0, -1)
    group = ConsumerGroup(broker, "g", "orders")
    group.join("c1")
    assert values(group.poll("c1")) == ["3"]  # resumes from the committed offset


def test_compaction_keeps_newest_value_per_key_and_tombstones() -> None:
    broker, _ = make_broker(partitions=1)
    producer = Producer(broker, "p1")
    for key, value in [("a", "1"), ("b", "2"), ("a", "3"), ("c", "4"), ("b", None), ("a", "5")]:
        producer.send("orders", value=value, key=key)
    broker.append("orders", 0, None, "unkeyed")
    assert broker.compact("orders") == 3
    kept = broker.fetch("orders", 0, 0)
    assert [(r.key, r.value, r.offset) for r in kept] == [
        ("c", "4", 3),
        ("b", None, 4),
        ("a", "5", 5),
        (None, "unkeyed", 6),
    ]
    assert values(broker.fetch("orders", 0, 1)) == ["4", None, "5", "unkeyed"]  # gap-tolerant
    assert broker.offsets("orders", 0) == (0, 7)  # offsets are never renumbered


def test_retention_moves_log_start_and_reset_policy_applies() -> None:
    broker, clock = make_broker(partitions=1, retention=60)
    producer = Producer(broker, "p1")
    producer.send("orders", value="old-0", key="k")
    producer.send("orders", value="old-1", key="k")
    clock.advance(100)
    producer.send("orders", value="new-2", key="k")
    stale = ConsumerGroup(broker, "stale", "orders")
    stale.join("c1")
    assert values(stale.poll("c1", max_records=1)) == ["old-0"]
    assert broker.expire("orders") == 2
    assert broker.offsets("orders", 0) == (2, 3)
    with pytest.raises(OffsetOutOfRangeError):
        broker.fetch("orders", 0, 1)
    assert values(stale.poll("c1")) == ["new-2"]  # position 1 was deleted: reset to earliest
    latest = ConsumerGroup(broker, "latest", "orders", auto_offset_reset="latest")
    latest.join("c1")
    assert latest.poll("c1") == []
    producer.send("orders", value="new-3", key="k")
    assert values(latest.poll("c1")) == ["new-3"]
    assert broker.expire("orders") == 0


@pytest.mark.parametrize(
    "action",
    [
        lambda b: b.create_topic("t", partitions=0),
        lambda b: b.create_topic("t", retention=0),
        lambda b: b.create_topic("orders"),
        lambda b: b.fetch("orders", 9, 0),
        lambda b: b.fetch("orders", 0, 0, limit=0),
        lambda b: b.fetch("nope", 0, 0),
        lambda b: ConsumerGroup(b, "g", "orders", auto_offset_reset="middle"),
        lambda b: Producer(b, "p").send("orders", "v", attempts=0),
    ],
)
def test_validation_errors(action) -> None:
    broker, _ = make_broker()
    with pytest.raises((ValidationError, ConflictError, NotFoundError)):
        action(broker)


def test_concurrent_producers_keep_per_key_order() -> None:
    broker, _ = make_broker(partitions=4)

    def produce(i: int) -> None:
        producer = Producer(broker, f"p{i}")
        for n in range(50):
            producer.send("orders", value=f"p{i}:{n}", key=f"p{i}", attempts=2)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(produce, range(16)))
    seen: dict[str, list[int]] = {}
    for p in range(4):
        for record in broker.fetch("orders", p, 0, limit=10_000):
            assert record.value is not None and record.key is not None
            seen.setdefault(record.key, []).append(int(record.value.split(":")[1]))
    assert sum(len(v) for v in seen.values()) == 16 * 50  # retries never duplicated
    assert all(order == list(range(50)) for order in seen.values())


def test_concurrent_join_leave_and_poll_keep_the_group_consistent() -> None:
    broker, _ = make_broker(partitions=6)
    producer = Producer(broker, "p1")
    for i in range(60):
        producer.send("orders", value=str(i), key=str(i))
    group = ConsumerGroup(broker, "g", "orders")
    group.join("anchor")

    def churn(i: int) -> int:
        member = f"m{i}"
        group.join(member)
        polled = group.poll(member, max_records=5)
        group.commit(member)
        group.leave(member)
        return len(polled)

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(churn, range(40)))
    assert all(0 <= c <= 5 for c in counts)
    assert group.assignment("anchor") == list(range(6))
    assert group.generation == 1 + 2 * 40
    # every record is either committed by a churner or redelivered to the survivor, never both
    lag = group.lag()
    remaining = group.poll("anchor", max_records=1_000)
    assert len(remaining) == sum(lag.values()) <= 60
    assert Counter(r.partition for r in remaining) == {p: n for p, n in lag.items() if n}
    assert group.poll("anchor") == []
