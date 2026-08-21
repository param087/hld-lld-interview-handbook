"""Catalog, cart, inventory rows, orders and the order transition table."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class OrderStatus(StrEnum):
    CREATED = "created"  # stock is held, money is not taken yet
    PAID = "paid"  # stock committed, ready to pick
    PACKED = "packed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"


class HoldStatus(StrEnum):
    HELD = "held"  # units moved from available to reserved, TTL running
    COMMITTED = "committed"  # units have left the warehouse
    RELEASED = "released"  # given back deliberately
    EXPIRED = "expired"  # given back by the sweeper


class PaymentStatus(StrEnum):
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    VOIDED = "voided"
    REFUNDED = "refunded"


class ShippingSpeed(StrEnum):
    STANDARD = "standard"
    EXPRESS = "express"


#: The order lifecycle in one dict. ``OrderService.transition`` is the only gate,
#: and the state diagram on the page is a drawing of exactly this table.
ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.PAID, OrderStatus.CANCELLED}),
    OrderStatus.PAID: frozenset({OrderStatus.PACKED, OrderStatus.CANCELLED}),
    OrderStatus.PACKED: frozenset({OrderStatus.SHIPPED, OrderStatus.CANCELLED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.DELIVERED: frozenset({OrderStatus.RETURNED}),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.RETURNED: frozenset(),
}

#: Cancelling from these states must give the held or committed units back.
RESTOCK_ON_CANCEL_FROM = frozenset({OrderStatus.CREATED, OrderStatus.PAID, OrderStatus.PACKED})
# --8<-- [end:enums]


# --8<-- [start:errors]
class OutOfStockError(ConflictError):
    """Not enough units of a SKU to satisfy the request. The oversell guard."""


class HoldExpiredError(ConflictError):
    """The reservation TTL elapsed and the units went back on the shelf."""


class OrderStateError(InvalidStateError):
    """The order is not in a state that allows this transition."""


class PriceChangedError(ConflictError):
    """The basket total moved between the cart page and the checkout call."""


class CheckoutInProgressError(ConflictError):
    """Another request with the same idempotency key is still running."""


class PaymentDeclinedError(ConflictError):
    """The gateway refused the charge."""


class UnknownSkuError(NotFoundError):
    """No SKU with that id."""


class UnknownOrderError(NotFoundError):
    """No order with that id."""


# --8<-- [end:errors]


# --8<-- [start:catalog]
@dataclass(frozen=True, slots=True)
class Category:
    id: str
    name: str
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class Product:
    id: str
    title: str
    category_id: str
    brand: str = ""


@dataclass(frozen=True, slots=True)
class Sku:
    """The unit you actually sell: one product plus its variant attributes."""

    id: str
    product_id: str
    price: Money
    attributes: tuple[tuple[str, str], ...] = ()
    weight_grams: int = 500

    def label(self) -> str:
        return f"{self.id} ({', '.join(f'{k}={v}' for k, v in self.attributes)})" if self.attributes else self.id


@dataclass(frozen=True, slots=True)
class Warehouse:
    id: str
    name: str
    region: str


@dataclass(frozen=True, slots=True)
class Address:
    line1: str
    city: str
    postcode: str
    country: str
    region: str = ""


@dataclass(slots=True)
class Cart:
    """SKU ids and quantities. It holds no prices, which is the whole point."""

    id: str
    customer_id: str | None = None  # None means a guest cart
    lines: dict[str, int] = field(default_factory=dict)

    def add(self, sku_id: str, quantity: int = 1) -> None:
        if quantity <= 0:
            raise ValidationError("quantity must be positive")
        self.lines[sku_id] = self.lines.get(sku_id, 0) + quantity

    def set_quantity(self, sku_id: str, quantity: int) -> None:
        if quantity < 0:
            raise ValidationError("quantity cannot be negative")
        if quantity == 0:
            self.lines.pop(sku_id, None)
        else:
            self.lines[sku_id] = quantity

    def remove(self, sku_id: str) -> None:
        self.lines.pop(sku_id, None)

    def is_empty(self) -> bool:
        return not self.lines

    def merge_from(self, guest: Cart) -> None:
        """Sign-in merge: quantities add up, and the guest cart is left untouched."""
        for sku_id, quantity in guest.lines.items():
            self.lines[sku_id] = self.lines.get(sku_id, 0) + quantity


# --8<-- [end:catalog]


# --8<-- [start:inventory_row]
@dataclass(slots=True)
class InventoryItem:
    """One SKU in one warehouse: what is on the shelf and what is spoken for.

    ``available`` and ``reserved`` are the two numbers the whole problem turns
    on, and ``version`` is the row version a SQL ``UPDATE ... WHERE version = ?``
    would check. In this process the per-SKU lock plays that role, but the
    counter is still here: it is how a caller that read the row earlier finds out
    the row moved underneath it, and it is the exact field you would carry into
    a database implementation.
    """

    sku_id: str
    warehouse_id: str
    available: int = 0
    reserved: int = 0
    version: int = 0

    def hold(self, quantity: int, expected_version: int | None = None) -> None:
        self._check_version(expected_version)
        if quantity <= 0:
            raise ValidationError("quantity must be positive")
        if quantity > self.available:
            raise OutOfStockError(f"{self.sku_id}@{self.warehouse_id}: {self.available} left, wanted {quantity}")
        self.available -= quantity
        self.reserved += quantity
        self.version += 1

    def commit(self, quantity: int, expected_version: int | None = None) -> None:
        """The units left the building. They are gone from both counters."""
        self._check_version(expected_version)
        if quantity > self.reserved:
            raise ConflictError(f"{self.sku_id}@{self.warehouse_id}: only {self.reserved} reserved")
        self.reserved -= quantity
        self.version += 1

    def release(self, quantity: int, expected_version: int | None = None) -> None:
        self._check_version(expected_version)
        if quantity > self.reserved:
            raise ConflictError(f"{self.sku_id}@{self.warehouse_id}: only {self.reserved} reserved")
        self.reserved -= quantity
        self.available += quantity
        self.version += 1

    def restock(self, quantity: int) -> None:
        if quantity <= 0:
            raise ValidationError("restock quantity must be positive")
        self.available += quantity
        self.version += 1

    def _check_version(self, expected_version: int | None) -> None:
        if expected_version is not None and expected_version != self.version:
            raise ConflictError(
                f"{self.sku_id}@{self.warehouse_id} moved: version {self.version}, expected {expected_version}"
            )


@dataclass(frozen=True, slots=True)
class HoldLine:
    sku_id: str
    warehouse_id: str
    quantity: int


@dataclass(slots=True)
class StockHold:
    """An all-or-nothing reservation across SKUs and warehouses, with a deadline."""

    id: str
    owner: str  # the checkout key or order id that owns these units
    lines: tuple[HoldLine, ...]
    created_at: float
    expires_at: float
    status: HoldStatus = HoldStatus.HELD

    def is_live(self, now: float) -> bool:
        return self.status is HoldStatus.HELD and now < self.expires_at

    def units(self) -> int:
        return sum(line.quantity for line in self.lines)


# --8<-- [end:inventory_row]


# --8<-- [start:order]
@dataclass(frozen=True, slots=True)
class OrderItem:
    """A price snapshot. The catalog may re-price tomorrow; this line never does."""

    sku_id: str
    title: str
    unit_price: Money
    quantity: int

    @property
    def line_total(self) -> Money:
        return self.unit_price * self.quantity


@dataclass(slots=True)
class Shipment:
    id: str
    order_id: str
    warehouse_id: str
    carrier: str
    tracking: str


@dataclass(slots=True)
class Payment:
    id: str
    order_id: str
    amount: Money
    status: PaymentStatus = PaymentStatus.AUTHORIZED


@dataclass(slots=True)
class Order:
    id: str
    customer_id: str
    items: tuple[OrderItem, ...]
    ship_to: Address
    discount: Money
    tax: Money
    shipping: Money
    hold_id: str
    idempotency_key: str
    placed_at: float
    status: OrderStatus = OrderStatus.CREATED
    shipment_id: str | None = None

    @property
    def subtotal(self) -> Money:
        total = Money(0)
        for item in self.items:
            total = total + item.line_total
        return total

    @property
    def total(self) -> Money:
        return self.subtotal - self.discount + self.tax + self.shipping

    def can_move_to(self, target: OrderStatus) -> bool:
        return target in ORDER_TRANSITIONS[self.status]


@dataclass(frozen=True, slots=True)
class Event:
    """What travels on the bus: a topic and a flat payload, nothing else."""

    topic: str
    at: float
    payload: dict[str, str] = field(default_factory=dict)


# --8<-- [end:order]
