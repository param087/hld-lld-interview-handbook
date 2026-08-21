"""Event Bus: topic routing, wildcards, error isolation, cancellation, worker-thread delivery."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, InvalidStateError, ValidationError
from patterns.event_bus import (
    EmailNotifier,
    Event,
    EventBus,
    Handler,
    InventoryService,
    simple_bus,
)

ORDER = {"order_id": "o-1", "lines": {"sku-1": 2}}


def collecting(bus: EventBus, pattern: str) -> list[Event]:
    seen: list[Event] = []
    bus.subscribe(pattern, seen.append)
    return seen


def test_exact_topic_routing_numbers_events_and_stamps_the_injected_clock() -> None:
    clock = FakeClock(start=50.0)
    bus = EventBus(clock)
    placed, paid = collecting(bus, "order.placed"), collecting(bus, "order.paid")
    assert bus.publish("order.placed", ORDER) == 1
    clock.advance(5)
    assert bus.publish("order.paid", ORDER) == 1
    assert [e.seq for e in placed] == [1] and [e.seq for e in paid] == [2]
    assert paid[0].published_at == 55.0 and placed[0].payload == ORDER
    assert placed[0].payload is not ORDER  # the bus copies, so a publisher cannot mutate it afterwards


def test_wildcard_subscription_sees_every_matching_topic_and_nothing_else() -> None:
    bus = EventBus(FakeClock())
    every_order = collecting(bus, "order.*")
    bus.publish("order.placed", ORDER)
    bus.publish("order.paid", ORDER)
    bus.publish("payment.failed", ORDER)
    assert [e.topic for e in every_order] == ["order.placed", "order.paid"]
    assert bus.subscriber_count("order.refunded") == 1 and bus.subscriber_count("payment.failed") == 0


def test_a_failing_handler_is_reported_and_the_others_still_run() -> None:
    failures: list[tuple[str, int, str]] = []
    bus = EventBus(FakeClock(), on_error=lambda h, e, exc: failures.append((e.topic, e.seq, str(exc))))

    def broken(event: Event) -> None:
        raise RuntimeError("audit store unavailable")

    bus.subscribe("order.placed", broken)
    after = collecting(bus, "order.placed")
    assert bus.publish("order.placed", ORDER) == 2
    assert len(after) == 1 and failures == [("order.placed", 1, "audit store unavailable")]


def test_cancel_is_idempotent_and_safe_from_inside_a_handler() -> None:
    bus = EventBus(FakeClock())
    seen: list[int] = []

    def once(event: Event) -> None:
        seen.append(event.seq)
        subscription.cancel()  # a handler may unsubscribe itself mid-dispatch

    subscription = bus.subscribe("order.placed", once)
    later = collecting(bus, "order.placed")
    bus.publish("order.placed", ORDER)
    bus.publish("order.placed", ORDER)
    assert seen == [1] and [e.seq for e in later] == [1, 2]
    subscription.cancel()
    assert bus.subscriber_count("order.placed") == 1


def test_unrouted_events_are_counted_not_lost_silently() -> None:
    bus = EventBus(FakeClock())
    assert bus.publish("payment.failed", ORDER) == 0 and bus.unrouted == 1
    collecting(bus, "payment.failed")
    assert bus.publish("payment.failed", ORDER) == 1 and bus.unrouted == 1


def test_worker_bus_delivers_off_thread_in_publish_order_and_join_waits() -> None:
    delivered: list[tuple[int, str]] = []
    gate = threading.Event()

    def handler(event: Event) -> None:
        gate.wait(timeout=2.0)
        delivered.append((event.seq, threading.current_thread().name))

    with EventBus(FakeClock(), workers=1) as bus:
        bus.subscribe("order.paid", handler)
        for _ in range(5):
            bus.publish("order.paid", ORDER)
        assert delivered == []  # publish returned without waiting for the handler
        gate.set()
        bus.join()
    assert [seq for seq, _ in delivered] == [1, 2, 3, 4, 5]
    assert {name for _, name in delivered} == {"EventBus-worker-0"}


def test_worker_survives_a_failing_handler_and_close_refuses_new_events() -> None:
    failures: list[str] = []
    bus = EventBus(FakeClock(), workers=1, on_error=lambda h, e, exc: failures.append(repr(exc)))
    seen = collecting(bus, "order.paid")

    def broken(event: Event) -> None:
        raise ValueError("boom")

    bus.subscribe("order.paid", broken)
    bus.publish("order.paid", ORDER)
    bus.publish("order.paid", ORDER)
    bus.close()  # drains both events, then stops the worker
    assert [e.seq for e in seen] == [1, 2] and failures == ["ValueError('boom')"] * 2
    with pytest.raises(InvalidStateError):
        bus.publish("order.paid", ORDER)
    bus.close()  # idempotent


def test_domain_subscribers_react_without_knowing_each_other() -> None:
    bus = EventBus(FakeClock())
    inventory, email = InventoryService(), EmailNotifier()
    bus.subscribe("order.placed", inventory.on_order_placed)
    bus.subscribe("order.*", email.on_order_event)
    bus.publish("order.placed", {"order_id": "o-7", "lines": {"sku-1": 2, "sku-2": 1}})
    bus.publish("order.paid", {"order_id": "o-7"})
    assert inventory.reserved() == {"sku-1": 2, "sku-2": 1}
    assert email.sent == ["order.placed -> o-7", "order.paid -> o-7"]


def test_concurrent_publishers_get_unique_sequence_numbers_and_every_event_is_delivered() -> None:
    bus = EventBus(FakeClock())
    seen: list[int] = []
    seen_lock = threading.Lock()

    def handler(event: Event) -> None:
        with seen_lock:
            seen.append(event.seq)

    bus.subscribe("tick", handler)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: bus.publish("tick"), range(400)))
    assert sorted(seen) == list(range(1, 401))


@pytest.mark.parametrize("workers", [-1, -5])
def test_validation_of_workers_and_patterns(workers: int) -> None:
    with pytest.raises(ValidationError):
        EventBus(FakeClock(), workers=workers)
    with pytest.raises(ValidationError):
        EventBus(FakeClock()).subscribe("", lambda event: None)


def test_simple_bus_routes_by_exact_topic_only() -> None:
    subscribe, publish = simple_bus()
    seen: list[str] = []
    handler: Handler = lambda event: seen.append(event.topic)  # noqa: E731
    subscribe("order.placed", handler)
    assert publish(Event("order.placed", ORDER, 1, 0.0)) == 1
    assert publish(Event("order.paid", ORDER, 2, 0.0)) == 0  # no wildcards, no unrouted count
    assert seen == ["order.placed"]
