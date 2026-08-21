"""SOLID: each test asserts what the refactor bought, not that the code still runs."""

from decimal import Decimal

import pytest

from common import ConflictError, Money, NotFoundError, ValidationError
from fundamentals.solid import (
    CardGateway,
    CashDrawer,
    Charges,
    CheckoutService,
    CheckoutServiceBefore,
    FlatShipping,
    FreeOverThreshold,
    InMemoryOrderRepository,
    Order,
    OrderLine,
    OrderServiceBefore,
    OrderValidator,
    PaymentGatewayBefore,
    RecordingNotifier,
    Rectangle,
    RectangleBefore,
    Refunds,
    Shape,
    ShippingCalculatorBefore,
    ShippingMethod,
    ShippingRule,
    Square,
    SquareBefore,
    TaxPolicy,
    build_checkout,
    default_shipping_registry,
    total_area,
)

LINES = (OrderLine("SKU-1", 2, Money.of("19.99")), OrderLine("SKU-2", 1, Money.of("5.00")))


def an_order(shipping: ShippingMethod = ShippingMethod.EXPRESS, lines=LINES) -> Order:
    return Order("ORD-1", "ana@example.com", lines, shipping)


def test_srp_the_split_preserves_the_arithmetic_of_the_god_class() -> None:
    order = an_order()
    legacy_total = OrderServiceBefore().place(order)
    receipt = build_checkout(CardGateway(), RecordingNotifier()).place(order, token="tok")
    assert receipt.total == legacy_total
    assert receipt.subtotal + receipt.shipping + receipt.tax == receipt.total


def test_srp_one_collaborator_changes_without_touching_the_others() -> None:
    order = an_order()
    zero_tax = CheckoutService(
        validator=OrderValidator(),
        shipping=default_shipping_registry(),
        tax=TaxPolicy(rate=Decimal("0")),
        payments=CardGateway(),
        orders=InMemoryOrderRepository(),
        notifier=RecordingNotifier(),
    )
    receipt = zero_tax.place(order, token="tok")
    assert receipt.tax == Money(0) and receipt.total == receipt.subtotal + receipt.shipping


@pytest.mark.parametrize(
    ("lines", "email"),
    [((), "ana@example.com"), (LINES, "ana-at-example.com")],
)
def test_srp_validation_lives_in_exactly_one_place(lines: tuple, email: str) -> None:
    order = Order("ORD-2", email, lines)
    with pytest.raises(ValidationError):
        OrderValidator().check(order)


def test_ocp_the_ladder_cannot_express_a_rule_the_registry_absorbs() -> None:
    bulk = (OrderLine("SKU-3", 6, Money.of("3.00")),)
    locker = Order("ORD-3", "bo@example.com", bulk, ShippingMethod.LOCKER)
    assert ShippingCalculatorBefore().cost(locker) == Money.of("2.49")  # flat, always
    assert default_shipping_registry().cost_for(locker) == Money.of("4.50")  # 6 x 0.75


def test_ocp_a_new_rule_is_registered_not_edited_in() -> None:
    registry = default_shipping_registry()
    order = an_order()
    assert registry.cost_for(order) == Money.of("9.99")
    registry.register(
        ShippingMethod.EXPRESS,
        FreeOverThreshold(FlatShipping(Money.of("9.99")), threshold=Money.of("40.00")),
    )
    assert registry.cost_for(order) == Money(0)  # 44.98 subtotal clears the threshold
    small = an_order(lines=(OrderLine("SKU-4", 1, Money.of("5.00")),))
    assert registry.cost_for(small) == Money.of("9.99")  # the wrapped rule still applies
    assert isinstance(FlatShipping(Money(0)), ShippingRule)


def rectangle_contract(shape: RectangleBefore) -> None:
    """The contract a Rectangle promises: setting one side leaves the other alone."""
    shape.set_width(5)
    shape.set_height(4)
    assert shape.area() == 20


def test_lsp_the_subclass_fails_its_base_class_contract_test() -> None:
    rectangle_contract(RectangleBefore(2, 3))  # the base class keeps its promise
    with pytest.raises(AssertionError):
        rectangle_contract(SquareBefore(4))  # the "is a" subclass does not


@pytest.mark.parametrize("shape", [Rectangle(5, 4), Square(4), Rectangle(1, 1)])
def test_lsp_every_shape_satisfies_the_same_contract(shape: Shape) -> None:
    assert shape.area() > 0
    assert total_area([shape, Square(2)]) == shape.area() + 4


def test_isp_the_fat_abc_forces_stubs_that_the_client_never_calls() -> None:
    class DrawerOnCharges(PaymentGatewayBefore):
        def charge(self, amount: Money, token: str) -> str:
            return "cash-1"

    with pytest.raises(TypeError):
        DrawerOnCharges()  # type: ignore[abstract] - four unwanted methods still missing


def test_isp_small_protocols_let_a_class_implement_only_what_it_can_do() -> None:
    drawer, gateway = CashDrawer(), CardGateway()
    assert isinstance(drawer, Charges) and not isinstance(drawer, Refunds)
    assert isinstance(gateway, Charges) and isinstance(gateway, Refunds)
    charge_id = gateway.charge(Money.of("10.00"), "tok")
    gateway.refund(charge_id)
    with pytest.raises(NotFoundError):
        gateway.refund(charge_id)


def test_dip_the_same_service_runs_on_any_charges_implementation() -> None:
    order = an_order()
    ids = [build_checkout(p, RecordingNotifier()).place(order, "tok").charge_id
           for p in (CardGateway(), CashDrawer())]
    assert ids == ["ch-1", "cash-1"]


def test_dip_the_declined_path_needs_a_two_line_fake_and_saves_nothing() -> None:
    orders = InMemoryOrderRepository()
    notifier = RecordingNotifier()
    checkout = CheckoutService(
        validator=OrderValidator(),
        shipping=default_shipping_registry(),
        tax=TaxPolicy(),
        payments=CardGateway(approve=False),
        orders=orders,
        notifier=notifier,
    )
    with pytest.raises(ConflictError):
        checkout.place(an_order(), token="tok-declined")
    with pytest.raises(NotFoundError):
        orders.get("ORD-1")  # charge first, then commit: nothing was persisted
    assert notifier.sent == []


def test_dip_the_before_version_cannot_be_given_a_test_double() -> None:
    service = CheckoutServiceBefore()
    assert isinstance(service._payments, CardGateway)  # hard-wired at construction
    assert service.place(an_order(), token="tok") == "ch-1"
