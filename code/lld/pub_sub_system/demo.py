"""One topic, two partitions, two groups, a retry, a dead letter and a replay."""

from common import FakeClock, SequentialIdGenerator
from lld.pub_sub_system.consumers import FlakyConsumer, RecordingConsumer
from lld.pub_sub_system.models import RetentionPolicy, RetryPolicy
from lld.pub_sub_system.services import Broker

ORDERS = "orders"
EVENTS = [
    ("alice", "created"),
    ("bob", "created"),
    ("alice", "paid"),
    ("bob", "cancelled"),
    ("alice", "shipped"),
    ("carol", "created"),
]


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    broker = Broker(
        clock=clock,
        ids=SequentialIdGenerator("m"),
        retry=RetryPolicy(max_attempts=3, base_delay=0.001),
    )
    broker.create_topic(ORDERS, partitions=2, retention=RetentionPolicy(max_messages=64))

    billing = RecordingConsumer("billing-1")
    audit = RecordingConsumer("audit-1")
    shipping = FlakyConsumer("shipping-1", fail_times=1, poison="bob:cancelled")
    broker.subscribe(ORDERS, "billing", billing)
    broker.subscribe(ORDERS, "audit", audit)
    broker.subscribe(ORDERS, "shipping", shipping)

    for key, event in EVENTS:
        record = broker.publish(ORDERS, f"{key}:{event}", key=key)
        print(f"published {record.payload:<18} -> partition {record.partition} offset {record.offset}")
    broker.drain()

    print(f"billing received {len(billing.records())} of {len(EVENTS)} (its own cursor)")
    print(f"audit   received {len(audit.records())} of {len(EVENTS)} (an independent cursor)")
    print(f"alice keeps her order (one partition, one worker): {billing.keys_in_order('alice')}")
    print(f"shipping stats: {broker._groups[('shipping', ORDERS)].stats()}")
    print(f"shipping retried alice:paid {shipping.attempts('alice:paid')} times before it acked")
    for letter in broker.dlq.letters():
        print(f"dead letter after {letter.attempts} attempts: {letter.record.payload} ({letter.error})")

    print(f"lag before replay: billing={broker.lag('billing', ORDERS)}")
    broker.replay("billing", ORDERS, from_offset=0)
    billing.wait_for(2 * len(EVENTS))
    broker.drain()
    print(f"billing after replay from offset 0: {len(billing.records())} records")

    broker.close()
    print(f"broker state={broker.state}, dead letters={len(broker.dlq)}")


if __name__ == "__main__":
    main()
