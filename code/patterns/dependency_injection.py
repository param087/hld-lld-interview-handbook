"""Dependency Injection: collaborators arrive through the constructor, chosen by the caller.

The running example is placing an order. ``OrderService`` needs a store, a payment
gateway, a notifier, a clock and an id generator; it declares each as a ``Protocol``
parameter and constructs none of them. ``main()`` is the composition root that wires
the graph together; the tests pass fakes (``FakeClock``, ``SequentialIdGenerator``,
``FakePaymentGateway``, ``RecordingNotifier``) to the same constructor and get a
deterministic service without patching. The last section restates the service as a
function whose collaborators are bound once with ``functools.partial``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol, runtime_checkable

from common import (
    Clock,
    ConflictError,
    FakeClock,
    HandbookError,
    IdGenerator,
    Money,
    SequentialIdGenerator,
    SystemClock,
    UuidIdGenerator,
    ValidationError,
)


# --8<-- [start:ports]
class PaymentDeclinedError(HandbookError):
    """The gateway refused the charge. Nothing is stored and nobody is notified."""


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    customer_id: str
    amount: Money
    placed_at: float
    payment_ref: str


@runtime_checkable
class OrderRepository(Protocol):
    def add(self, order: Order) -> None: ...  # ConflictError on a reused id

    def get(self, order_id: str) -> Order | None: ...


@runtime_checkable
class PaymentGateway(Protocol):
    def charge(self, customer_id: str, amount: Money, *, idempotency_key: str) -> str: ...


@runtime_checkable
class Notifier(Protocol):
    def send(self, customer_id: str, message: str) -> None: ...


# --8<-- [end:ports]


# --8<-- [start:service]
class OrderService:
    """The consumer: every collaborator is a constructor parameter typed as a Protocol.

    The service builds nothing it depends on and reads no global. Time and identity
    arrive as ``Clock`` and ``IdGenerator`` from ``common`` instead of ``time.time()``
    and ``uuid4()``, so a test controls both; the store, the gateway and the notifier
    arrive the same way, so a test can observe every side effect.
    """

    def __init__(
        self,
        orders: OrderRepository,
        payments: PaymentGateway,
        notifier: Notifier,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._orders = orders
        self._payments = payments
        self._notifier = notifier
        self._clock = clock
        self._ids = ids

    def place_order(self, customer_id: str, amount: Money) -> Order:
        if amount.cents <= 0:
            raise ValidationError("amount must be positive")
        order_id = self._ids.next_id()
        reference = self._payments.charge(customer_id, amount, idempotency_key=order_id)
        order = Order(order_id, customer_id, amount, self._clock.now(), reference)
        self._orders.add(order)
        self._notifier.send(customer_id, f"order {order_id} confirmed: {amount}")
        return order


# --8<-- [end:service]


# --8<-- [start:implementations]
class InMemoryOrderRepository:
    """A dict behind the contract: the store for tests, demos and small deployments.

    ``_lock`` protects ``_orders`` so the duplicate check and the insert are one step.
    """

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._lock = threading.Lock()

    def add(self, order: Order) -> None:
        with self._lock:
            if order.id in self._orders:
                raise ConflictError(f"order {order.id} already exists")
            self._orders[order.id] = order

    def get(self, order_id: str) -> Order | None:
        with self._lock:
            return self._orders.get(order_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._orders)


class LoggingNotifier:
    """A production notifier for a handbook with no SMTP server: it logs, via an injected logger."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def send(self, customer_id: str, message: str) -> None:
        self._logger.info("to %s: %s", customer_id, message)


class FakePaymentGateway:
    """A test double you own: a stub for the outcome, a spy for the calls.

    No ``unittest.mock``: a misspelt method on a hand-written fake fails loudly, while
    ``Mock()`` auto-creates it and the test passes for the wrong reason.
    """

    def __init__(self, *, decline: bool = False) -> None:
        self.decline = decline
        self.charges: list[tuple[str, Money, str]] = []

    def charge(self, customer_id: str, amount: Money, *, idempotency_key: str) -> str:
        if self.decline:
            raise PaymentDeclinedError(f"card declined for {customer_id}")
        self.charges.append((customer_id, amount, idempotency_key))
        return f"pay-{idempotency_key}"


class RecordingNotifier:
    """A spy: keeps every message so a test can assert on what was said, and to whom."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, customer_id: str, message: str) -> None:
        self.sent.append((customer_id, message))


# --8<-- [end:implementations]


# --8<-- [start:functional]
type PlaceOrder = Callable[[str, Money], Order]


def place_order(
    orders: OrderRepository,
    payments: PaymentGateway,
    notifier: Notifier,
    clock: Clock,
    ids: IdGenerator,
    customer_id: str,
    amount: Money,
) -> Order:
    """The service as a function: collaborators first, then the request. Same body, no ``self``."""
    if amount.cents <= 0:
        raise ValidationError("amount must be positive")
    order_id = ids.next_id()
    reference = payments.charge(customer_id, amount, idempotency_key=order_id)
    order = Order(order_id, customer_id, amount, clock.now(), reference)
    orders.add(order)
    notifier.send(customer_id, f"order {order_id} confirmed: {amount}")
    return order


def bind(
    orders: OrderRepository,
    payments: PaymentGateway,
    notifier: Notifier,
    clock: Clock,
    ids: IdGenerator,
) -> PlaceOrder:
    """``partial`` is constructor injection for functions: bind the collaborators once."""
    return partial(place_order, orders, payments, notifier, clock, ids)


# --8<-- [end:functional]


def main() -> None:
    print("--- production wiring: the composition root alone names the concrete classes ---")
    live = OrderService(
        orders=InMemoryOrderRepository(),
        payments=FakePaymentGateway(),  # the real one is an HTTP adapter; see the Adapter page
        notifier=LoggingNotifier(logging.getLogger("orders")),
        clock=SystemClock(),
        ids=UuidIdGenerator(),
    )
    order = live.place_order("ada", Money.of("25.00"))
    print(f"placed an order with a {len(order.id)}-char uuid at wall-clock time; nothing below depends on either")

    print("--- test wiring: the same class, a fake in every slot ---")
    orders, payments, notifier = InMemoryOrderRepository(), FakePaymentGateway(), RecordingNotifier()
    clock, ids = FakeClock(start=1_700_000_000.0), SequentialIdGenerator("order")
    service = OrderService(orders, payments, notifier, clock, ids)
    first = service.place_order("ada", Money.of("25.00"))
    print(f"{first.id} for {first.customer_id}: {first.amount} at t={first.placed_at:.0f}, paid as {first.payment_ref}")
    print(f"notifier recorded: {notifier.sent[-1]}")
    clock.advance(60)
    second = service.place_order("bob", Money.of("40.00"))
    print(f"{second.id} placed {second.placed_at - first.placed_at:.0f} s later: the clock is a collaborator")

    print("--- functional variant: partial() binds the collaborators once ---")
    place = bind(orders, payments, notifier, clock, ids)
    third = place("cy", Money.of("5.00"))
    print(f"{third.id} for {third.customer_id}: {third.amount}; orders stored: {len(orders)}")

    print("--- a declined payment stores nothing and notifies nobody ---")
    declined = OrderService(orders, FakePaymentGateway(decline=True), notifier, clock, ids)
    try:
        declined.place_order("dee", Money.of("15.00"))
    except PaymentDeclinedError as exc:
        print(f"rejected: {exc}; orders stored: {len(orders)}; messages sent: {len(notifier.sent)}")


if __name__ == "__main__":
    main()
