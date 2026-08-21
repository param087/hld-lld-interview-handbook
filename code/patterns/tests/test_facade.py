"""Facade: one call runs the workflow, failures are compensated, the subsystem owns its invariants."""

from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from common import (
    FakeClock,
    InvalidStateError,
    Money,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)
from patterns.facade import (
    CardDeclinedError,
    CartLine,
    CheckoutFacade,
    InventoryService,
    Notifier,
    OrderRepository,
    OrderStatus,
    OutOfStockError,
    PaymentService,
    place_order,
)

KEYBOARD = CartLine("keyboard", 2, Money.of("60.00"))
MOUSE = CartLine("mouse", 1, Money.of("25.00"))


class Subsystem:
    """The four parts plus a facade over them, built fresh for every test."""

    def __init__(self, keyboard: int = 5, mouse: int = 10) -> None:
        self.inventory = InventoryService({"keyboard": keyboard, "mouse": mouse})
        self.payments = PaymentService(SequentialIdGenerator("pay"))
        self.orders = OrderRepository()
        self.notifier = Notifier()
        self.clock = FakeClock(1_700_000_000)
        self.facade = CheckoutFacade(
            self.inventory, self.payments, self.orders, self.notifier, SequentialIdGenerator("ord"), self.clock
        )


def test_one_call_reserves_charges_saves_and_notifies() -> None:
    sub = Subsystem()
    order = sub.facade.place_order("cust-1", [KEYBOARD, MOUSE], "tok-visa")
    assert order.total == Money.of("145.00")
    assert order.status is OrderStatus.PLACED
    assert order.placed_at == 1_700_000_000
    assert order.payment_id == "pay-1"
    assert (sub.inventory.available("keyboard"), sub.inventory.available("mouse")) == (3, 9)
    assert sub.orders.get(order.order_id) is order
    assert sub.notifier.sent == ["cust-1: order ord-1 placed: 145.00 USD"]


def test_declined_card_releases_the_reservation_and_leaves_no_trace() -> None:
    sub = Subsystem()
    with pytest.raises(CardDeclinedError):
        sub.facade.place_order("cust-1", [KEYBOARD, MOUSE], "declined-card")
    assert (sub.inventory.available("keyboard"), sub.inventory.available("mouse")) == (5, 10)
    assert len(sub.orders) == 0
    assert sub.notifier.sent == []
    assert sub.payments.charges == 0


def test_out_of_stock_and_empty_cart_fail_before_any_money_moves() -> None:
    sub = Subsystem(keyboard=1)
    with pytest.raises(OutOfStockError, match="keyboard"):
        sub.facade.place_order("cust-1", [KEYBOARD, MOUSE], "tok-visa")
    assert sub.inventory.available("mouse") == 10  # the whole reservation is rejected, not half of it
    with pytest.raises(ValidationError):
        sub.facade.place_order("cust-1", [], "tok-visa")
    assert sub.payments.charges == 0 and len(sub.orders) == 0


def test_cancel_refunds_restocks_and_cannot_run_twice() -> None:
    sub = Subsystem()
    order = sub.facade.place_order("cust-1", [KEYBOARD, MOUSE], "tok-visa")
    cancelled = sub.facade.cancel_order(order.order_id)
    assert cancelled is order and order.status is OrderStatus.CANCELLED
    assert sub.payments.charges == 0
    assert (sub.inventory.available("keyboard"), sub.inventory.available("mouse")) == (5, 10)
    assert sub.notifier.sent[-1] == "cust-1: order ord-1 cancelled: 145.00 USD refunded"
    with pytest.raises(InvalidStateError):
        sub.facade.cancel_order(order.order_id)
    with pytest.raises(NotFoundError):
        sub.facade.cancel_order("ord-999")


def test_the_subsystem_stays_reachable_beside_the_facade() -> None:
    sub = Subsystem()
    sub.inventory.reserve([MOUSE])  # a client with a narrower need talks to the part directly
    assert sub.inventory.available("mouse") == 9
    order = sub.facade.place_order("cust-1", [MOUSE], "tok-visa")
    assert order.total == Money.of("25.00") and sub.inventory.available("mouse") == 8


def test_concurrent_checkouts_never_oversell_because_the_inventory_owns_the_lock() -> None:
    sub = Subsystem(keyboard=5, mouse=100)
    one_keyboard = [CartLine("keyboard", 1, Money.of("60.00"))]

    def attempt(customer: int) -> bool:
        try:
            sub.facade.place_order(f"cust-{customer}", one_keyboard, "tok-visa")
        except OutOfStockError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(40)))
    assert outcomes.count(True) == 5 and outcomes.count(False) == 35
    assert sub.inventory.available("keyboard") == 0
    assert sub.payments.charges == 5 and len(sub.orders) == 5


def test_function_form_bound_with_partial_matches_the_class() -> None:
    sub = Subsystem()
    quick_checkout = partial(
        place_order,
        inventory=sub.inventory,
        payments=sub.payments,
        orders=sub.orders,
        notifier=sub.notifier,
        ids=SequentialIdGenerator("ord"),
        clock=sub.clock,
    )
    order = quick_checkout("cust-2", [MOUSE], "tok-visa")
    assert order.order_id == "ord-1" and order.total == Money.of("25.00")
    assert sub.orders.get("ord-1") is order
    with pytest.raises(CardDeclinedError):
        quick_checkout("cust-2", [MOUSE], "declined-card")
    assert sub.inventory.available("mouse") == 9
