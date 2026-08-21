"""In-process pub/sub and the notification fan-out that rides on it."""

from __future__ import annotations

import threading
from collections.abc import Callable

from lld.food_delivery.models import Event

Handler = Callable[[Event], None]

ORDER_TOPICS = (
    "order.placed",
    "order.accepted",
    "order.rejected",
    "order.ready",
    "order.assigned",
    "order.picked_up",
    "order.delivered",
    "order.cancelled",
)


# --8<-- [start:bus]
class EventBus:
    """Topic to handlers, dispatched synchronously with per-handler error isolation.

    The lock only guards the subscriber map and the failure log; handlers run
    *outside* it, so a slow notification cannot stall the order that published it.
    A failing handler is recorded and the next one still runs -- an order must not
    roll back because a push notification timed out.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handlers: dict[str, list[Handler]] = {}
        self._failures: list[tuple[str, str]] = []

    def subscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            self._handlers.setdefault(topic, []).append(handler)

    def publish(self, event: Event) -> int:
        with self._lock:
            handlers = list(self._handlers.get(event.topic, ()))
        delivered = 0
        for handler in handlers:
            try:
                handler(event)
                delivered += 1
            except Exception as exc:  # isolation is the whole point of a bus
                with self._lock:
                    self._failures.append((event.topic, str(exc)))
        return delivered

    def failures(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._failures)


# --8<-- [end:bus]


class NotificationService:
    """Observer over the bus: one inbox per recipient, no polling anywhere.

    ``OrderService`` never imports this class. Adding an SMS channel or a
    restaurant tablet feed is one more subscriber, not an edit to the order flow.
    """

    TEMPLATES: dict[str, str] = {
        "order.placed": "we sent {order_id} to {restaurant}",
        "order.accepted": "{restaurant} is cooking {order_id}",
        "order.rejected": "{restaurant} could not take {order_id}",
        "order.ready": "{order_id} is on the counter",
        "order.assigned": "{partner} is bringing {order_id}",
        "order.picked_up": "{partner} picked up {order_id}",
        "order.delivered": "{order_id} delivered by {partner}",
        "order.cancelled": "{order_id} was cancelled",
    }

    def __init__(self, bus: EventBus) -> None:
        self._lock = threading.Lock()
        self._inboxes: dict[str, list[str]] = {}
        for topic in ORDER_TOPICS:
            bus.subscribe(topic, self._on_event)

    def _on_event(self, event: Event) -> None:
        template = self.TEMPLATES.get(event.topic, event.topic)
        message = template.format_map(_Blank(event.payload))
        recipients = [event.payload.get(key) for key in ("customer_id", "restaurant", "partner")]
        with self._lock:
            for recipient in recipients:
                if recipient:
                    self._inboxes.setdefault(recipient, []).append(message)

    def inbox(self, recipient: str) -> list[str]:
        with self._lock:
            return list(self._inboxes.get(recipient, ()))


class _Blank(dict):
    """``format_map`` helper: a missing key renders as ``-`` instead of raising."""

    def __missing__(self, key: str) -> str:
        return "-"
