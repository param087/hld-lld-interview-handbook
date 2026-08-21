"""Dependency Injection: the constructor is the seam and the fakes are the proof."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from common import ConflictError, FakeClock, Money, SequentialIdGenerator, ValidationError
from patterns.dependency_injection import (
    FakePaymentGateway,
    InMemoryOrderRepository,
    Notifier,
    Order,
    OrderRepository,
    OrderService,
    PaymentDeclinedError,
    PaymentGateway,
    RecordingNotifier,
    bind,
)

EPOCH = 1_700_000_000.0


@dataclass(slots=True)
class Wiring:
    """The test's composition root: the same constructor, a fake in every slot."""

    orders: InMemoryOrderRepository
    payments: FakePaymentGateway
    notifier: RecordingNotifier
    clock: FakeClock
    ids: SequentialIdGenerator

    def service(self) -> OrderService:
        return OrderService(self.orders, self.payments, self.notifier, self.clock, self.ids)


@pytest.fixture
def wiring() -> Wiring:
    return Wiring(
        InMemoryOrderRepository(),
        FakePaymentGateway(),
        RecordingNotifier(),
        FakeClock(start=EPOCH),
        SequentialIdGenerator("order"),
    )


def test_fakes_make_every_output_deterministic(wiring: Wiring) -> None:
    order = wiring.service().place_order("ada", Money.of("25.00"))
    assert order == Order("order-1", "ada", Money.of("25.00"), EPOCH, "pay-order-1")
    assert wiring.orders.get("order-1") == order
    assert wiring.payments.charges == [("ada", Money.of("25.00"), "order-1")]
    assert wiring.notifier.sent == [("ada", "order order-1 confirmed: 25.00 USD")]


def test_time_is_injected_so_a_test_can_move_it(wiring: Wiring) -> None:
    service = wiring.service()
    first = service.place_order("ada", Money.of("1.00"))
    wiring.clock.advance(60)
    second = service.place_order("ada", Money.of("1.00"))
    assert second.placed_at - first.placed_at == 60
    assert (first.id, second.id) == ("order-1", "order-2")


def test_declined_payment_stores_nothing_and_notifies_nobody(wiring: Wiring) -> None:
    service = OrderService(
        wiring.orders, FakePaymentGateway(decline=True), wiring.notifier, wiring.clock, wiring.ids
    )
    with pytest.raises(PaymentDeclinedError):
        service.place_order("bob", Money.of("40.00"))
    assert len(wiring.orders) == 0 and wiring.notifier.sent == []


def test_validation_happens_before_any_collaborator_is_touched(wiring: Wiring) -> None:
    service = wiring.service()
    with pytest.raises(ValidationError):
        service.place_order("ada", Money.of("0.00"))
    assert wiring.payments.charges == [] and wiring.notifier.sent == []
    assert service.place_order("ada", Money.of("1.00")).id == "order-1"  # no id was consumed


def test_a_double_needs_no_base_class_only_the_shape() -> None:
    class ApprovesEverything:
        def charge(self, customer_id: str, amount: Money, *, idempotency_key: str) -> str:
            return "approved"

    class Silent:
        def send(self, customer_id: str, message: str) -> None:
            return None

    doubles: list[tuple[object, type]] = [
        (ApprovesEverything(), PaymentGateway),
        (Silent(), Notifier),
        (InMemoryOrderRepository(), OrderRepository),
    ]
    for double, protocol in doubles:
        assert isinstance(double, protocol)
        assert protocol not in type(double).__mro__
    service = OrderService(
        InMemoryOrderRepository(), ApprovesEverything(), Silent(), FakeClock(), SequentialIdGenerator()
    )
    assert service.place_order("ada", Money.of("9.99")).payment_ref == "approved"


def test_partial_binds_the_same_collaborators_the_constructor_would(wiring: Wiring) -> None:
    place = bind(wiring.orders, wiring.payments, wiring.notifier, wiring.clock, wiring.ids)
    via_function = place("ada", Money.of("25.00"))
    via_class = wiring.service().place_order("ada", Money.of("25.00"))
    assert (via_function.id, via_class.id) == ("order-1", "order-2")
    assert (via_function.payment_ref, via_class.payment_ref) == ("pay-order-1", "pay-order-2")
    assert len(wiring.orders) == 2 and len(wiring.notifier.sent) == 2


def test_repository_rejects_a_reused_id(wiring: Wiring) -> None:
    order = Order("order-1", "ada", Money.of("1.00"), EPOCH, "pay-order-1")
    wiring.orders.add(order)
    with pytest.raises(ConflictError):
        wiring.orders.add(order)
    assert len(wiring.orders) == 1


def test_concurrent_orders_all_land_with_unique_ids(wiring: Wiring) -> None:
    service = wiring.service()

    def place(n: int) -> Order:
        return service.place_order(f"customer-{n}", Money.of("1.00"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        orders = list(pool.map(place, range(200)))
    assert len({order.id for order in orders}) == 200
    assert len(wiring.orders) == 200
    assert len(wiring.payments.charges) == 200 and len(wiring.notifier.sent) == 200
