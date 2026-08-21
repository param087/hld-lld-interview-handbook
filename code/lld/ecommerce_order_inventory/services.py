"""Catalog, carts, orders and the shipment listener: the services around checkout."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock, ValidationError
from lld.ecommerce_order_inventory.events import ORDER_PAID, EventBus
from lld.ecommerce_order_inventory.inventory import InventoryService
from lld.ecommerce_order_inventory.models import (
    Cart,
    Category,
    Event,
    Order,
    OrderItem,
    OrderStateError,
    OrderStatus,
    Product,
    Shipment,
    Sku,
    UnknownOrderError,
    UnknownSkuError,
)
from lld.ecommerce_order_inventory.repository import InMemoryRepository
from lld.ecommerce_order_inventory.strategies import Specification


# --8<-- [start:catalog_service]
class CatalogService:
    """Products, SKUs and search. Search is a Specification, not ten keyword arguments."""

    def __init__(
        self,
        inventory: InventoryService,
        categories: Iterable[Category] = (),
        products: Iterable[Product] = (),
        skus: Iterable[Sku] = (),
    ) -> None:
        self._inventory = inventory
        self._categories = {c.id: c for c in categories}
        self._products = {p.id: p for p in products}
        self._skus = {s.id: s for s in skus}
        self._lock = threading.Lock()

    def add(self, sku: Sku) -> Sku:
        with self._lock:
            self._skus[sku.id] = sku
            return sku

    def sku(self, sku_id: str) -> Sku:
        with self._lock:
            try:
                return self._skus[sku_id]
            except KeyError:
                raise UnknownSkuError(f"no sku {sku_id}") from None

    def reprice(self, sku_id: str, price: Money) -> Sku:
        """Catalog prices move. Orders keep their snapshot; carts do not."""
        current = self.sku(sku_id)
        return self.add(Sku(current.id, current.product_id, price, current.attributes, current.weight_grams))

    def in_category(self, category_id: str) -> list[Sku]:
        product_ids = {p.id for p in self._products.values() if p.category_id == category_id}
        with self._lock:
            return sorted((s for s in self._skus.values() if s.product_id in product_ids), key=lambda s: s.id)

    def search(self, spec: Specification, category_id: str | None = None) -> list[Sku]:
        candidates = self.in_category(category_id) if category_id else sorted(self._skus.values(), key=lambda s: s.id)
        return [s for s in candidates if spec.is_satisfied_by(s, self._inventory.available(s.id))]


class CartService:
    """Carts, including the guest-to-customer merge that every store gets wrong once."""

    def __init__(self, ids: IdGenerator | None = None) -> None:
        self._ids = ids or SequentialIdGenerator("CART")
        self._carts: dict[str, Cart] = {}
        self._lock = threading.Lock()

    def open(self, customer_id: str | None = None) -> Cart:
        cart = Cart(self._ids.next_id(), customer_id)
        with self._lock:
            self._carts[cart.id] = cart
        return cart

    def cart(self, cart_id: str) -> Cart:
        with self._lock:
            try:
                return self._carts[cart_id]
            except KeyError:
                raise UnknownSkuError(f"no cart {cart_id}") from None

    def merge_guest_cart(self, guest_cart_id: str, customer_id: str) -> Cart:
        """Sign-in: quantities add up into the customer's cart, guest cart emptied."""
        guest = self.cart(guest_cart_id)
        with self._lock:
            target = next((c for c in self._carts.values() if c.customer_id == customer_id), None)
        if target is None:
            guest.customer_id = customer_id
            return guest
        target.merge_from(guest)
        guest.lines.clear()
        return target


# --8<-- [end:catalog_service]


# --8<-- [start:orders]
class OrderService:
    """Owns the order repository and every status change.

    ``transition`` is a check-and-flip against ``ORDER_TRANSITIONS`` under one
    lock, so two warehouse operators cannot both move an order out of ``PAID``.
    """

    def __init__(
        self,
        orders: InMemoryRepository | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self.repository = orders or InMemoryRepository("order")
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("ORD")
        self._lock = threading.Lock()

    def next_id(self) -> str:
        return self._ids.next_id()

    def order(self, order_id: str) -> Order:
        order = self.repository.find(order_id)
        if order is None:
            raise UnknownOrderError(f"unknown order {order_id}")
        return order

    def history(self, customer_id: str) -> list[Order]:
        return sorted(
            (o for o in self.repository.all() if o.customer_id == customer_id), key=lambda o: o.placed_at
        )

    def transition(self, order_id: str, target: OrderStatus) -> Order:
        with self._lock:
            order = self.order(order_id)
            if not order.can_move_to(target):
                raise OrderStateError(f"order {order_id} cannot move {order.status} to {target}")
            order.status = target
            return order

    def attach_shipment(self, order_id: str, shipment_id: str) -> Order:
        with self._lock:
            order = self.order(order_id)
            order.shipment_id = shipment_id
            return order

    def line_quantities(self, order: Order) -> dict[str, int]:
        return {item.sku_id: item.quantity for item in order.items}


class ShipmentDispatcher:
    """A bus subscriber, not a call in the checkout path: paid orders get a parcel.

    Creating a shipment is retryable and nobody is waiting on it, which is
    exactly the test for "should this be an event?".
    """

    CARRIER = "handbook-express"

    def __init__(self, bus: EventBus, orders: OrderService, ids: IdGenerator | None = None) -> None:
        self._orders = orders
        self._ids = ids or SequentialIdGenerator("SHP")
        self._lock = threading.Lock()
        self._shipments: dict[str, Shipment] = {}
        bus.subscribe(ORDER_PAID, self._on_order_paid)

    def _on_order_paid(self, event: Event) -> None:
        order_id = event.payload["order_id"]
        shipment = Shipment(
            id=self._ids.next_id(),
            order_id=order_id,
            warehouse_id=event.payload.get("warehouse_id", "-"),
            carrier=self.CARRIER,
            tracking=f"TRK-{order_id}",
        )
        with self._lock:
            self._shipments[order_id] = shipment
        self._orders.attach_shipment(order_id, shipment.id)

    def shipment_for(self, order_id: str) -> Shipment | None:
        with self._lock:
            return self._shipments.get(order_id)


# --8<-- [end:orders]


def snapshot_items(catalog: CatalogService, cart: Cart) -> tuple[OrderItem, ...]:
    """Freeze catalog prices into order lines. Called once, inside the checkout."""
    if cart.is_empty():
        raise ValidationError("cannot check out an empty cart")
    items = []
    for sku_id in sorted(cart.lines):
        sku = catalog.sku(sku_id)
        items.append(OrderItem(sku.id, sku.label(), sku.price, cart.lines[sku_id]))
    return tuple(items)
