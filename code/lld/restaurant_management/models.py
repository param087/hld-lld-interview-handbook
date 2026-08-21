"""Tables, orders, kitchen tickets and bills for the restaurant POS.

Three status enums drive the whole flow: ``TableStatus`` (the floor), ``OrderStatus``
(the tab) and ``TicketStatus`` (the kitchen). Keeping them separate is deliberate - a
table is occupied long after its food is ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class TableStatus(StrEnum):
    AVAILABLE = "available"
    RESERVED = "reserved"  # held for a booking in the next slot
    OCCUPIED = "occupied"
    CLEANING = "cleaning"  # party left, busser not finished


class OrderStatus(StrEnum):
    OPEN = "open"  # the server is still adding items
    SENT = "sent"  # a kitchen ticket exists; edits now need a void
    SERVED = "served"
    BILLED = "billed"
    CLOSED = "closed"  # paid in full


class TicketStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    READY = "ready"
    SERVED = "served"


class StaffRole(StrEnum):
    HOST = "host"
    SERVER = "server"
    COOK = "cook"
    MANAGER = "manager"


class PaymentMethod(StrEnum):
    CARD = "card"
    CASH = "cash"


# --8<-- [end:enums]


# --8<-- [start:errors]
class TableUnavailableError(ConflictError):
    """The requested tables are not all free, or they do not seat the party."""


class OrderStateError(InvalidStateError):
    """The order is not in a state that allows this edit."""


class TableStateError(InvalidStateError):
    """The table is not in a state that allows this transition."""


class UnknownItemError(NotFoundError):
    """Unknown table, order, menu component or ticket id."""


class ItemUnavailableError(ConflictError):
    """The kitchen has 86'd this dish for the evening."""


# --8<-- [end:errors]


# --8<-- [start:floor]
@dataclass(frozen=True, slots=True)
class Staff:
    id: str
    name: str
    role: StaffRole


@dataclass(frozen=True, slots=True)
class Shift:
    """A named service period; the daily report is grouped by it."""

    name: str
    starts_at: float
    ends_at: float

    def contains(self, when: float) -> bool:
        return self.starts_at <= when < self.ends_at


@dataclass(slots=True)
class Table:
    """One physical table. ``status`` is the contended field on a busy Friday."""

    id: str
    capacity: int
    status: TableStatus = TableStatus.AVAILABLE
    order_id: str | None = None
    reserved_for: str | None = None

    def reserve(self, reservation_id: str) -> None:
        if self.status is not TableStatus.AVAILABLE:
            raise TableStateError(f"table {self.id} is {self.status}, cannot be reserved")
        self.status = TableStatus.RESERVED
        self.reserved_for = reservation_id

    def occupy(self, order_id: str) -> None:
        if self.status not in (TableStatus.AVAILABLE, TableStatus.RESERVED):
            raise TableStateError(f"table {self.id} is {self.status}, cannot seat a party")
        self.status = TableStatus.OCCUPIED
        self.order_id = order_id
        self.reserved_for = None

    def start_cleaning(self) -> None:
        if self.status is not TableStatus.OCCUPIED:
            raise TableStateError(f"table {self.id} is {self.status}, not occupied")
        self.status = TableStatus.CLEANING
        self.order_id = None

    def mark_clean(self) -> None:
        if self.status is not TableStatus.CLEANING:
            raise TableStateError(f"table {self.id} is {self.status}, not being cleaned")
        self.status = TableStatus.AVAILABLE

    def release(self) -> None:
        """Cancel a reservation or roll back a partial seating."""
        if self.status not in (TableStatus.RESERVED, TableStatus.OCCUPIED):
            raise TableStateError(f"table {self.id} is {self.status}, nothing to release")
        self.status = TableStatus.AVAILABLE
        self.order_id = None
        self.reserved_for = None


@dataclass(slots=True)
class Reservation:
    id: str
    guest_name: str
    party_size: int
    slot_at: float
    table_ids: tuple[str, ...] = ()
    seated: bool = False


@dataclass(slots=True)
class WaitlistEntry:
    id: str
    guest_name: str
    party_size: int
    joined_at: float
    quoted_wait_minutes: int


# --8<-- [end:floor]


# --8<-- [start:order]
@dataclass(frozen=True, slots=True)
class Modifier:
    """Extra cheese, no onions, medium rare. Priced, so the bill stays honest."""

    name: str
    price_delta: Money = Money(0)


@dataclass(slots=True)
class OrderItem:
    id: str
    component_id: str
    name: str
    unit_price: Money  # snapshot: a menu price change must not move an open bill
    quantity: int = 1
    modifiers: tuple[Modifier, ...] = ()
    voided: bool = False

    def line_total(self) -> Money:
        unit = self.unit_price
        for modifier in self.modifiers:
            unit = unit + modifier.price_delta
        return unit * self.quantity


ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.OPEN: frozenset({OrderStatus.SENT, OrderStatus.CLOSED}),
    OrderStatus.SENT: frozenset({OrderStatus.OPEN, OrderStatus.SERVED}),
    OrderStatus.SERVED: frozenset({OrderStatus.BILLED}),
    OrderStatus.BILLED: frozenset({OrderStatus.CLOSED}),
    OrderStatus.CLOSED: frozenset(),
}


@dataclass(slots=True)
class Order:
    """One tab, one table (or one joined group of tables)."""

    id: str
    table_ids: tuple[str, ...]
    server_id: str
    opened_at: float
    items: list[OrderItem] = field(default_factory=list)
    status: OrderStatus = OrderStatus.OPEN

    def transition_to(self, target: OrderStatus) -> None:
        if target not in ORDER_TRANSITIONS[self.status]:
            raise OrderStateError(f"order {self.id}: {self.status} -> {target} is not allowed")
        self.status = target

    def item(self, item_id: str) -> OrderItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise UnknownItemError(f"order {self.id} has no line {item_id!r}")

    def live_items(self) -> list[OrderItem]:
        return [i for i in self.items if not i.voided]

    def subtotal(self) -> Money:
        total = Money(0)
        for item in self.live_items():
            total = total + item.line_total()
        return total


@dataclass(slots=True)
class KitchenTicket:
    id: str
    order_id: str
    table_ids: tuple[str, ...]
    lines: tuple[str, ...]
    created_at: float
    status: TicketStatus = TicketStatus.QUEUED


# --8<-- [end:order]


# --8<-- [start:bill]
@dataclass(frozen=True, slots=True)
class BillLine:
    description: str
    amount: Money


@dataclass(frozen=True, slots=True)
class Payment:
    id: str
    bill_id: str
    amount: Money
    method: PaymentMethod
    paid_at: float


@dataclass(frozen=True, slots=True)
class Bill:
    id: str
    order_id: str
    lines: tuple[BillLine, ...]
    subtotal: Money
    discount: Money
    tax: Money
    total: Money
    shares: tuple[Money, ...] = ()

    def shares_add_up(self) -> bool:
        """The invariant every split-bill implementation gets wrong at least once."""
        if not self.shares:
            return True
        summed = Money(0, self.total.currency)
        for share in self.shares:
            summed = summed + share
        return summed == self.total


def validate_party(party_size: int, capacity: int) -> None:
    if party_size < 1:
        raise ValidationError("a party needs at least one guest")
    if party_size > capacity:
        raise TableUnavailableError(f"party of {party_size} does not fit {capacity} seats")


# --8<-- [end:bill]
