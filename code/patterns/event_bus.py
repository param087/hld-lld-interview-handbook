"""Event Bus: in-process publish/subscribe by topic, delivered inline or by a worker thread.

The running example is an order flow. ``EventBus`` routes each published
``Event`` to the handlers subscribed to its topic, exactly (``order.placed``)
or by wildcard (``order.*``). ``InventoryService`` and ``EmailNotifier``
subscribe without knowing who publishes, and the checkout code publishes
without knowing who listens. Delivery runs on the publisher's thread by
default, or is queued and drained by worker threads when ``workers`` is set;
either way a handler that raises is reported and isolated, so the other
handlers and the worker survive. The last section is the ten-line bus (a
``defaultdict`` of callables) that is enough when nothing needs threads,
wildcards, cancellation or metrics.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from types import TracebackType
from typing import Any

from common import Clock, FakeClock, InvalidStateError, ValidationError

log = logging.getLogger(__name__)


# --8<-- [start:event]
@dataclass(frozen=True, slots=True)
class Event:
    """What travels: an immutable record, so every handler may keep it without copying."""

    topic: str
    payload: Mapping[str, Any]
    seq: int
    published_at: float


type Handler = Callable[[Event], None]
type ErrorHandler = Callable[[Handler, Event, Exception], None]


def log_handler_error(handler: Handler, event: Event, exc: Exception) -> None:
    """Default error policy: record and move on. Inject another to count, alert or re-raise."""
    log.warning("handler %r failed on %s #%d: %r", handler, event.topic, event.seq, exc)


@dataclass(frozen=True, slots=True)
class Delivery:
    """One unit of work for a worker thread: this handler, this event."""

    handler: Handler
    event: Event


class Subscription:
    """The handle ``subscribe`` returns. ``cancel`` is idempotent and safe from inside a handler."""

    def __init__(self, bus: EventBus, pattern: str, handler: Handler) -> None:
        self._bus = bus
        self.pattern = pattern
        self.handler = handler

    def cancel(self) -> None:
        self._bus.unsubscribe(self)


# --8<-- [end:event]


# --8<-- [start:bus]
class EventBus:
    """Topic-based pub/sub with synchronous or worker-thread delivery.

    ``_lock`` protects ``_subscribers``, ``_seq``, ``_unrouted`` and ``_closed``. The
    handler list is copied under the lock and handlers run outside it, so a slow
    handler never blocks ``subscribe`` and a handler may cancel its own subscription.
    With ``workers == 0``, ``publish`` runs every handler before returning. With
    ``workers >= 1`` it enqueues one ``Delivery`` per handler and returns at once;
    daemon threads drain the ``queue.Queue`` in FIFO order (one worker keeps events
    in publish order, more workers trade that order for throughput). A handler that
    raises goes to ``on_error``; the remaining handlers, and the worker, carry on.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        workers: int = 0,
        on_error: ErrorHandler = log_handler_error,
    ) -> None:
        if workers < 0:
            raise ValidationError("workers cannot be negative")
        self._clock = clock
        self._on_error = on_error
        self._subscribers: defaultdict[str, list[Subscription]] = defaultdict(list)
        self._lock = threading.Lock()
        self._seq = 0
        self._unrouted = 0
        self._closed = False
        self._queue: queue.Queue[Delivery | None] = queue.Queue()
        self._workers = [
            threading.Thread(target=self._work, name=f"EventBus-worker-{n}", daemon=True)
            for n in range(workers)
        ]
        for worker in self._workers:
            worker.start()

    def subscribe(self, pattern: str, handler: Handler) -> Subscription:
        """``pattern`` is a topic (``order.placed``) or a glob (``order.*``)."""
        if not pattern:
            raise ValidationError("a subscription needs a topic or a pattern")
        subscription = Subscription(self, pattern, handler)
        with self._lock:
            self._subscribers[pattern].append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            kept = [s for s in self._subscribers.get(subscription.pattern, ()) if s is not subscription]
            if kept:
                self._subscribers[subscription.pattern] = kept
            else:
                self._subscribers.pop(subscription.pattern, None)

    def subscriber_count(self, topic: str) -> int:
        return len(self._handlers_for(topic))

    @property
    def unrouted(self) -> int:
        """Events published to a topic nobody listens to: a metric worth alarming on."""
        with self._lock:
            return self._unrouted

    def publish(self, topic: str, payload: Mapping[str, Any] | None = None) -> int:
        """Route one event to every matching handler; returns how many handlers matched."""
        with self._lock:
            if self._closed:
                raise InvalidStateError("event bus is closed")
            self._seq += 1
            event = Event(topic, dict(payload or {}), self._seq, self._clock.now())
        handlers = self._handlers_for(topic)
        if not handlers:
            with self._lock:
                self._unrouted += 1
            return 0
        for handler in handlers:
            if self._workers:
                self._queue.put(Delivery(handler, event))
            else:
                self._deliver(Delivery(handler, event))
        return len(handlers)

    def _handlers_for(self, topic: str) -> list[Handler]:
        with self._lock:
            exact = [s.handler for s in self._subscribers.get(topic, ())]
            wild = [
                s.handler
                for pattern, subs in self._subscribers.items()
                if "*" in pattern and fnmatchcase(topic, pattern)
                for s in subs
            ]
        return exact + wild

    def _deliver(self, delivery: Delivery) -> None:
        """Error isolation: one failing handler cannot stop the others or kill a worker."""
        try:
            delivery.handler(delivery.event)
        except Exception as exc:
            try:
                self._on_error(delivery.handler, delivery.event, exc)
            except Exception:
                log.exception("on_error itself failed for %s #%d", delivery.event.topic, delivery.event.seq)

    def _work(self) -> None:
        while (delivery := self._queue.get()) is not None:
            self._deliver(delivery)
            self._queue.task_done()
        self._queue.task_done()

    def join(self) -> None:
        """Block until every queued delivery has been handled (tests, graceful shutdown)."""
        self._queue.join()

    def close(self) -> None:
        """Refuse new events, let the workers drain what is queued, then stop them."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join()

    def __enter__(self) -> EventBus:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# --8<-- [end:bus]


# --8<-- [start:subscribers]
class InventoryService:
    """Reserves stock when an order is placed; knows nothing about checkout or email.

    ``_lock`` protects ``_reserved``: with a worker-thread bus the handler runs off-thread.
    """

    def __init__(self) -> None:
        self._reserved: dict[str, int] = {}
        self._lock = threading.Lock()

    def on_order_placed(self, event: Event) -> None:
        lines: Mapping[str, int] = event.payload["lines"]
        with self._lock:
            for sku, quantity in lines.items():
                self._reserved[sku] = self._reserved.get(sku, 0) + quantity

    def reserved(self) -> dict[str, int]:
        with self._lock:
            return dict(self._reserved)


class EmailNotifier:
    """Subscribed to ``order.*``: one handler for every order event, present and future."""

    def __init__(self) -> None:
        self._sent: list[str] = []
        self._lock = threading.Lock()

    def on_order_event(self, event: Event) -> None:
        with self._lock:
            self._sent.append(f"{event.topic} -> {event.payload['order_id']}")

    @property
    def sent(self) -> list[str]:
        with self._lock:
            return list(self._sent)


# --8<-- [end:subscribers]


# --8<-- [start:pythonic]
type Subscribe = Callable[[str, Handler], None]
type Publish = Callable[[Event], int]


def simple_bus() -> tuple[Subscribe, Publish]:
    """The whole pattern in ten lines: a dict of lists and a loop.

    No threads, wildcards, cancellation, isolation or metrics; enough inside one
    module, in a test, or until the second team subscribes.
    """
    subscribers: defaultdict[str, list[Handler]] = defaultdict(list)

    def subscribe(topic: str, handler: Handler) -> None:
        subscribers[topic].append(handler)

    def publish(event: Event) -> int:
        handlers = list(subscribers[event.topic])
        for handler in handlers:
            handler(event)
        return len(handlers)

    return subscribe, publish


# --8<-- [end:pythonic]


def main() -> None:
    clock = FakeClock(start=1_000.0)
    failures: list[str] = []

    def remember(handler: Handler, event: Event, exc: Exception) -> None:
        failures.append(f"{event.topic} #{event.seq}: {exc!r}")

    def audit(event: Event) -> None:
        raise RuntimeError("audit store unavailable")

    inventory, email = InventoryService(), EmailNotifier()
    bus = EventBus(clock, on_error=remember)
    bus.subscribe("order.placed", inventory.on_order_placed)
    bus.subscribe("order.placed", audit)
    emails = bus.subscribe("order.*", email.on_order_event)

    print("--- synchronous bus: handlers run on the publisher's thread, failures are isolated ---")
    order = {"order_id": "o-100", "lines": {"sku-1": 2, "sku-2": 1}}
    for topic in ("order.placed", "order.paid", "payment.failed"):
        print(f"  {topic:<14} -> {bus.publish(topic, order)} handler(s)")
    print(f"  inventory reserved: {inventory.reserved()}")
    print(f"  emails: {email.sent}")
    print(f"  isolated failures: {failures}")
    print(f"  unrouted events: {bus.unrouted}")
    emails.cancel()
    print(f"  after emails.cancel(): order.shipped -> {bus.publish('order.shipped', order)} handler(s)")

    print("--- worker-thread bus: publish returns at once, delivery happens on the worker ---")
    gate = threading.Event()
    delivered_on: list[str] = []
    worker_failures: list[str] = []

    def slow_ledger(event: Event) -> None:
        gate.wait(timeout=2.0)  # a slow downstream: the publisher never waits for it
        delivered_on.append(threading.current_thread().name)

    with EventBus(clock, workers=1, on_error=lambda h, e, exc: worker_failures.append(repr(exc))) as async_bus:
        async_bus.subscribe("order.paid", slow_ledger)
        async_bus.subscribe("order.paid", audit)
        for _ in range(2):
            async_bus.publish("order.paid", order)
        print(f"  publisher on {threading.current_thread().name} continued; deliveries so far: {len(delivered_on)}")
        gate.set()
        async_bus.join()
        print(f"  after join(): {len(delivered_on)} deliveries on {delivered_on[0]}; worker survived {len(worker_failures)} failures")
    try:
        async_bus.publish("order.paid", order)
    except InvalidStateError as exc:
        print(f"  after close(): InvalidStateError: {exc}")

    print("--- pythonic: a defaultdict of callables is the whole bus ---")
    subscribe, publish = simple_bus()
    subscribe("order.placed", lambda event: print(f"  lambda saw {event.topic} #{event.seq}"))
    routed = publish(Event("order.placed", order, 1, clock.now()))
    print(f"  routed to {routed} handler(s)")


if __name__ == "__main__":
    main()
