"""Facade: one entry point that runs a workflow across a subsystem the client never sees.

The running example is checkout. The subsystem has four parts that know nothing
about each other: ``InventoryService`` reserves and releases stock,
``PaymentService`` charges and refunds, ``OrderRepository`` stores orders and
``Notifier`` tells the customer. ``CheckoutFacade`` is the one door:
``place_order`` and ``cancel_order`` run the steps in the right order and undo
the earlier ones when a later one fails. The subsystem stays reachable; the
facade is a convenience, not a wall.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import partial

from common import (
    Clock,
    FakeClock,
    HandbookError,
    IdGenerator,
    InvalidStateError,
    Money,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)


# --8<-- [start:subsystem]
class OrderStatus(StrEnum):
    PLACED = "placed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class CartLine:
    sku: str
    quantity: int
    unit_price: Money


@dataclass(slots=True)
class Order:
    order_id: str
    customer_id: str
    lines: tuple[CartLine, ...]
    total: Money
    payment_id: str
    status: OrderStatus
    placed_at: float


class OutOfStockError(HandbookError):
    """Raised by the inventory before any money moves."""


class CardDeclinedError(HandbookError):
    """Raised by the payment service; the facade must compensate for it."""


class InventoryService:
    """Stock per SKU. The lock makes check-and-decrement atomic across threads."""

    def __init__(self, stock: Mapping[str, int]) -> None:
        self._stock = dict(stock)
        self._lock = threading.Lock()

    def available(self, sku: str) -> int:
        with self._lock:
            return self._stock.get(sku, 0)

    def reserve(self, lines: Sequence[CartLine]) -> None:
        with self._lock:
            short = [line.sku for line in lines if self._stock.get(line.sku, 0) < line.quantity]
            if short:
                raise OutOfStockError(f"insufficient stock for {', '.join(short)}")
            for line in lines:
                self._stock[line.sku] -= line.quantity

    def release(self, lines: Sequence[CartLine]) -> None:
        with self._lock:
            for line in lines:
                self._stock[line.sku] = self._stock.get(line.sku, 0) + line.quantity


class PaymentService:
    """Charges and refunds against a card token; tokens starting with ``declined`` are declined."""

    def __init__(self, ids: IdGenerator) -> None:
        self._ids = ids
        self._charges: dict[str, Money] = {}

    def charge(self, customer_id: str, amount: Money, card_token: str) -> str:
        if card_token.startswith("declined"):
            raise CardDeclinedError(f"card declined for {customer_id}")
        payment_id = self._ids.next_id()
        self._charges[payment_id] = amount
        return payment_id

    def refund(self, payment_id: str) -> Money:
        try:
            return self._charges.pop(payment_id)
        except KeyError:
            raise NotFoundError(f"unknown payment {payment_id}") from None

    @property
    def charges(self) -> int:
        return len(self._charges)


class OrderRepository:
    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def save(self, order: Order) -> None:
        self._orders[order.order_id] = order

    def get(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise NotFoundError(f"unknown order {order_id}") from None

    def __len__(self) -> int:
        return len(self._orders)


class Notifier:
    """Stands in for e-mail: remembers what it sent so the demo and the tests can show it."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def notify(self, customer_id: str, text: str) -> None:
        self.sent.append(f"{customer_id}: {text}")


# --8<-- [end:subsystem]


# --8<-- [start:facade]
class CheckoutFacade:
    """The Facade: the workflow in one place, the subsystem behind it untouched.

    It owns the order of the steps and the compensation when a later step fails.
    It owns no business rule that belongs to a part: stock arithmetic stays in the
    inventory, declines stay in payments. Clients that need a single part can still
    call it directly; the facade adds a door, it does not remove the others.
    """

    def __init__(
        self,
        inventory: InventoryService,
        payments: PaymentService,
        orders: OrderRepository,
        notifier: Notifier,
        ids: IdGenerator,
        clock: Clock,
    ) -> None:
        self._inventory = inventory
        self._payments = payments
        self._orders = orders
        self._notifier = notifier
        self._ids = ids
        self._clock = clock

    def place_order(self, customer_id: str, cart: Sequence[CartLine], card_token: str) -> Order:
        if not cart:
            raise ValidationError("cart is empty")
        total = sum((line.unit_price * line.quantity for line in cart), Money(0))
        self._inventory.reserve(cart)  # fails before any money moves
        try:
            payment_id = self._payments.charge(customer_id, total, card_token)
        except CardDeclinedError:
            self._inventory.release(cart)  # compensate: undo the step that did succeed
            raise
        order = Order(
            self._ids.next_id(), customer_id, tuple(cart), total, payment_id,
            OrderStatus.PLACED, self._clock.now(),
        )
        self._orders.save(order)
        self._notifier.notify(customer_id, f"order {order.order_id} placed: {total}")
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order.status is not OrderStatus.PLACED:
            raise InvalidStateError(f"order {order_id} is already {order.status}")
        refunded = self._payments.refund(order.payment_id)
        self._inventory.release(order.lines)
        order.status = OrderStatus.CANCELLED
        self._orders.save(order)
        self._notifier.notify(order.customer_id, f"order {order_id} cancelled: {refunded} refunded")
        return order


# --8<-- [end:facade]


# --8<-- [start:pythonic]
# The facade holds no state of its own, so a function with keyword-only collaborators is the
# same thing; ``functools.partial`` binds the subsystem once and hands back a one-step callable.
def place_order(
    customer_id: str,
    cart: Sequence[CartLine],
    card_token: str,
    *,
    inventory: InventoryService,
    payments: PaymentService,
    orders: OrderRepository,
    notifier: Notifier,
    ids: IdGenerator,
    clock: Clock,
) -> Order:
    facade = CheckoutFacade(inventory, payments, orders, notifier, ids, clock)
    return facade.place_order(customer_id, cart, card_token)


# --8<-- [end:pythonic]


def main() -> None:
    inventory = InventoryService({"keyboard": 5, "mouse": 10})
    payments = PaymentService(SequentialIdGenerator("pay"))
    orders = OrderRepository()
    notifier = Notifier()
    checkout = CheckoutFacade(
        inventory, payments, orders, notifier, SequentialIdGenerator("ord"), FakeClock(1_700_000_000)
    )
    cart = [CartLine("keyboard", 2, Money.of("60.00")), CartLine("mouse", 1, Money.of("25.00"))]

    def stock() -> str:
        return f"keyboard {inventory.available('keyboard')}, mouse {inventory.available('mouse')}"

    print("--- one call runs reserve, charge, save, notify ---")
    order = checkout.place_order("cust-1", cart, "tok-visa")
    print(f"{order.order_id} for {order.customer_id}: {order.total}, {order.payment_id}, {order.status}")
    print(f"stock after: {stock()}")

    print("--- the facade compensates: a declined card releases the reservation ---")
    try:
        checkout.place_order("cust-1", cart, "declined-card")
    except CardDeclinedError as exc:
        print(f"CardDeclinedError: {exc}; stock back to {stock()}; orders saved: {len(orders)}")

    print("--- and it checks before it spends: out of stock means no charge at all ---")
    try:
        checkout.place_order("cust-1", [CartLine("keyboard", 4, Money.of("60.00"))], "tok-visa")
    except OutOfStockError as exc:
        print(f"OutOfStockError: {exc}; charges on record: {payments.charges}")

    print("--- cancel reverses the workflow ---")
    cancelled = checkout.cancel_order(order.order_id)
    print(f"{cancelled.order_id} {cancelled.status}; stock {stock()}; charges on record: {payments.charges}")
    try:
        checkout.cancel_order(order.order_id)
    except InvalidStateError as exc:
        print(f"InvalidStateError: {exc}")

    print("--- the subsystem is still reachable when the facade is too coarse ---")
    print(f"inventory.available('mouse') -> {inventory.available('mouse')}")

    print("--- what the notifier sent along the way ---")
    for line in notifier.sent:
        print(line)

    print("--- Pythonic variant: the facade as a function, the subsystem bound once ---")
    quick_checkout = partial(
        place_order, inventory=inventory, payments=payments, orders=orders, notifier=notifier,
        ids=SequentialIdGenerator("ord", start=2), clock=FakeClock(1_700_000_000),
    )
    order = quick_checkout("cust-2", [CartLine("mouse", 1, Money.of("25.00"))], "tok-visa")
    print(f"{order.order_id} for {order.customer_id}: {order.total}; stock {stock()}")


if __name__ == "__main__":
    main()
