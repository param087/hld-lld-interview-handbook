"""Entities, value objects, enums, the order transition table and domain errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import cos, hypot, radians

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError

EARTH_RADIUS_KM = 6371.0


# --8<-- [start:enums]
class OrderStatus(StrEnum):
    PLACED = "placed"  # paid for (authorized), waiting for the restaurant
    ACCEPTED = "accepted"
    PREPARING = "preparing"
    READY = "ready"  # on the counter, waiting for a courier
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"  # customer pulled out
    REJECTED = "rejected"  # restaurant refused


class PartnerStatus(StrEnum):
    OFFLINE = "offline"
    IDLE = "idle"  # available for an offer
    OFFERED = "offered"  # holds one live lease and cannot be offered again
    DELIVERING = "delivering"


class OfferStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"  # the lease timed out
    VOIDED = "voided"  # the order disappeared under the courier


class PaymentStatus(StrEnum):
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    VOIDED = "voided"
    REFUNDED = "refunded"


class PaymentMethod(StrEnum):
    CARD = "card"
    WALLET = "wallet"
    CASH = "cash"


#: The single source of truth for the order lifecycle. Every service asks this
#: table rather than writing its own if/elif ladder, and the state diagram on the
#: page is a drawing of exactly this dict.
ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PLACED: frozenset({OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.CANCELLED}),
    OrderStatus.ACCEPTED: frozenset({OrderStatus.PREPARING, OrderStatus.CANCELLED}),
    OrderStatus.PREPARING: frozenset({OrderStatus.READY, OrderStatus.CANCELLED}),
    OrderStatus.READY: frozenset({OrderStatus.PICKED_UP}),
    OrderStatus.PICKED_UP: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}

#: A courier may be attached only while the food still needs moving. Dispatch
#: starts at ACCEPTED on purpose: the courier should arrive as the food does.
ASSIGNABLE_STATUSES = frozenset({OrderStatus.ACCEPTED, OrderStatus.PREPARING, OrderStatus.READY})
# --8<-- [end:enums]


# --8<-- [start:errors]
class RestaurantClosedError(ConflictError):
    """The restaurant is not taking orders right now."""


class ItemUnavailableError(ConflictError):
    """An item in the cart went off the menu between browsing and checkout."""


class OrderStateError(InvalidStateError):
    """The order is not in a state that allows this transition."""


class OfferStateError(InvalidStateError):
    """The offer is expired, already answered, or belongs to a different courier."""


class NoPartnerAvailableError(ConflictError):
    """Nobody is idle within range who has not already passed on this order."""


class PaymentDeclinedError(ConflictError):
    """The gateway refused the authorization."""


class UnknownOrderError(NotFoundError):
    """No order with that id."""


class UnknownPartnerError(NotFoundError):
    """No delivery partner with that id."""


# --8<-- [end:errors]


@dataclass(frozen=True, slots=True)
class Location:
    lat: float
    lon: float

    def distance_km(self, other: Location) -> float:
        """Equirectangular approximation: exact enough inside one city, and cheap."""
        mean_lat = radians((self.lat + other.lat) / 2)
        dx = radians(other.lon - self.lon) * cos(mean_lat) * EARTH_RADIUS_KM
        dy = radians(other.lat - self.lat) * EARTH_RADIUS_KM
        return hypot(dx, dy)


# --8<-- [start:catalog]
@dataclass(slots=True)
class MenuItem:
    id: str
    name: str
    price: Money
    available: bool = True


@dataclass(slots=True)
class Menu:
    items: dict[str, MenuItem] = field(default_factory=dict)

    def add(self, item: MenuItem) -> MenuItem:
        self.items[item.id] = item
        return item

    def item(self, item_id: str) -> MenuItem:
        try:
            return self.items[item_id]
        except KeyError:
            raise NotFoundError(f"no menu item {item_id}") from None


@dataclass(slots=True)
class Restaurant:
    id: str
    name: str
    location: Location
    prep_minutes: int = 20
    is_open: bool = True
    menu: Menu = field(default_factory=Menu)


@dataclass(slots=True)
class Cart:
    """Item ids and quantities only. Prices are resolved at checkout, never here."""

    customer_id: str
    restaurant_id: str | None = None
    lines: dict[str, int] = field(default_factory=dict)

    def add(self, restaurant_id: str, item_id: str, quantity: int = 1) -> None:
        if quantity <= 0:
            raise ValidationError("quantity must be positive")
        if self.restaurant_id is not None and self.restaurant_id != restaurant_id:
            raise ValidationError("a cart holds items from one restaurant only")
        self.restaurant_id = restaurant_id
        self.lines[item_id] = self.lines.get(item_id, 0) + quantity

    def remove(self, item_id: str) -> None:
        self.lines.pop(item_id, None)
        if not self.lines:
            self.restaurant_id = None

    def is_empty(self) -> bool:
        return not self.lines


# --8<-- [end:catalog]


# --8<-- [start:order]
@dataclass(frozen=True, slots=True)
class OrderItem:
    """A price snapshot. The menu may change tomorrow; this line never does."""

    item_id: str
    name: str
    unit_price: Money
    quantity: int

    @property
    def line_total(self) -> Money:
        return self.unit_price * self.quantity


@dataclass(slots=True)
class Order:
    id: str
    customer_id: str
    restaurant_id: str
    items: tuple[OrderItem, ...]
    deliver_to: Location
    delivery_fee: Money
    discount: Money
    placed_at: float
    status: OrderStatus = OrderStatus.PLACED
    partner_id: str | None = None
    coupon_code: str | None = None

    @property
    def subtotal(self) -> Money:
        total = Money(0)
        for item in self.items:
            total = total + item.line_total
        return total

    @property
    def total(self) -> Money:
        return self.subtotal + self.delivery_fee - self.discount

    def can_move_to(self, target: OrderStatus) -> bool:
        return target in ORDER_TRANSITIONS[self.status]

    def is_assignable(self) -> bool:
        return self.status in ASSIGNABLE_STATUSES


@dataclass(slots=True)
class DeliveryPartner:
    id: str
    name: str
    location: Location
    rating: float = 5.0
    ratings_count: int = 0
    status: PartnerStatus = PartnerStatus.OFFLINE
    deliveries_today: int = 0
    current_order_id: str | None = None

    def is_free(self) -> bool:
        return self.status is PartnerStatus.IDLE


@dataclass(slots=True)
class DeliveryOffer:
    """A time-boxed lease on one courier. Exactly one can be live per courier."""

    id: str
    order_id: str
    partner_id: str
    created_at: float
    expires_at: float
    status: OfferStatus = OfferStatus.PENDING

    def is_live(self, now: float) -> bool:
        return self.status is OfferStatus.PENDING and now < self.expires_at


# --8<-- [end:order]


@dataclass(slots=True)
class Payment:
    id: str
    order_id: str
    amount: Money
    method: PaymentMethod
    status: PaymentStatus = PaymentStatus.AUTHORIZED


@dataclass(frozen=True, slots=True)
class Rating:
    order_id: str
    partner_id: str
    stars: int
    comment: str = ""

    def __post_init__(self) -> None:
        if not 1 <= self.stars <= 5:
            raise ValidationError("stars must be between 1 and 5")


@dataclass(frozen=True, slots=True)
class Event:
    """What travels on the bus: a topic and a flat payload, nothing else."""

    topic: str
    at: float
    payload: dict[str, str] = field(default_factory=dict)
