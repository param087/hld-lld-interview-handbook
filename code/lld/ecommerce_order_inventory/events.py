"""The event bus and the three things that listen to it."""

from __future__ import annotations

import threading
from collections.abc import Callable

from lld.ecommerce_order_inventory.inventory import InventoryService
from lld.ecommerce_order_inventory.models import Event

Handler = Callable[[Event], None]

ORDER_PLACED = "order.placed"
ORDER_PAID = "order.paid"
ORDER_SHIPPED = "order.shipped"
ORDER_CANCELLED = "order.cancelled"
STOCK_LOW = "stock.low"
TOPICS = (ORDER_PLACED, ORDER_PAID, ORDER_SHIPPED, ORDER_CANCELLED, STOCK_LOW)


# --8<-- [start:bus]
class EventBus:
    """Topic to handlers, dispatched synchronously with per-handler error isolation.

    The lock guards the subscriber map and the failure log only. Handlers run
    *outside* it, so a slow listener never stalls the checkout that published the
    event, and a listener that raises is recorded rather than propagated -- an
    order must not fail because a low-stock alert did.

    What is deliberately **not** on this bus: reserving and committing stock.
    Those must be synchronous and ordered with the payment, so they are direct
    calls inside the checkout. The bus carries the effects you can afford to
    retry later: restock alerts, notifications, shipment creation.
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


# --8<-- [start:subscribers]
class LowStockMonitor:
    """Inventory's ear on the bus: every placed order re-checks the reorder point.

    This is the part of inventory that *is* event-driven. Reserving stock has to
    happen inside the checkout; noticing that a SKU dropped below its reorder
    point does not, and running it here keeps the checkout path short.
    """

    def __init__(self, bus: EventBus, inventory: InventoryService, threshold: int = 3) -> None:
        self._bus = bus
        self._inventory = inventory
        self._threshold = threshold
        self._lock = threading.Lock()
        self._alerts: list[tuple[str, int]] = []
        bus.subscribe(ORDER_PLACED, self._on_order_placed)

    def _on_order_placed(self, event: Event) -> None:
        for sku_id in event.payload.get("sku_ids", "").split(","):
            if not sku_id:
                continue
            free = self._inventory.available(sku_id)
            if free <= self._threshold:
                with self._lock:
                    self._alerts.append((sku_id, free))
                self._bus.publish(Event(STOCK_LOW, event.at, {"sku_id": sku_id, "available": str(free)}))

    def alerts(self) -> list[tuple[str, int]]:
        with self._lock:
            return list(self._alerts)


class NotificationService:
    """One inbox per customer, filled by whatever the bus carries."""

    TEMPLATES: dict[str, str] = {
        ORDER_PLACED: "order {order_id} received, total {total}",
        ORDER_PAID: "payment taken for {order_id}",
        ORDER_SHIPPED: "{order_id} is on its way, tracking {tracking}",
        ORDER_CANCELLED: "{order_id} was cancelled",
    }

    def __init__(self, bus: EventBus) -> None:
        self._lock = threading.Lock()
        self._inboxes: dict[str, list[str]] = {}
        for topic in self.TEMPLATES:
            bus.subscribe(topic, self._on_event)

    def _on_event(self, event: Event) -> None:
        customer = event.payload.get("customer_id")
        if not customer:
            return
        message = self.TEMPLATES[event.topic].format_map(_Blank(event.payload))
        with self._lock:
            self._inboxes.setdefault(customer, []).append(message)

    def inbox(self, customer_id: str) -> list[str]:
        with self._lock:
            return list(self._inboxes.get(customer_id, ()))


class _Blank(dict):
    """``format_map`` helper: a missing key renders as ``-`` instead of raising."""

    def __missing__(self, key: str) -> str:
        return "-"


# --8<-- [end:subscribers]
