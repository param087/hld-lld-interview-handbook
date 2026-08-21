"""Design principles beyond SOLID: the responsibilities land where the principles say."""

import pytest

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError
from fundamentals.principles import (
    Address,
    CheckoutService,
    Customer,
    DestinationTax,
    DiscountRate,
    InMemoryOrderRepository,
    LoyaltyAccount,
    Order,
    TaxRate,
    flat_shipping,
    free_over,
    percentage_of,
)


def make_order(order_id: str = "O-1", postcode: str = "94107") -> Order:
    customer = Customer("C-1", "Ada", Address("1 Market St", "San Francisco", postcode))
    order = Order(order_id, customer)
    order.add_line("SKU-1", Money.of("29.99"), 2)
    order.add_line("SKU-2", Money.of("9.99"), 1)
    return order


@pytest.mark.parametrize(
    ("cents", "basis_points", "expected"),
    [
        (10_000, 875, 875),  # 100.00 at 8.75%
        (8_996, 875, 787),  # 78.715 cents rounds half-up to 787
        (1, 5_000, 1),  # half a cent rounds up, once, in one place
        (10_000, 0, 0),
    ],
)
def test_one_rounding_rule_serves_every_percentage(cents: int, basis_points: int, expected: int) -> None:
    amount = Money(cents)
    assert percentage_of(amount, basis_points) == Money(expected)
    # Tax and discount are separate types for separate reasons, but share the rule.
    assert TaxRate(basis_points).on(amount) == DiscountRate(basis_points, "spring").on(amount)


def test_a_negative_rate_is_rejected_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        percentage_of(Money(100), -1)


def test_the_order_owns_the_arithmetic_and_hides_the_customer_graph() -> None:
    order = make_order()
    assert order.subtotal() == Money.of("69.97")  # 29.99 x 2 + 9.99
    assert order.ship_to_postcode() == "94107"
    assert order.lines[0].subtotal == Money.of("59.98")
    with pytest.raises(AttributeError):
        order.lines.append(order.lines[0])  # type: ignore[attr-defined]


@pytest.mark.parametrize("quantity", [0, -3])
def test_an_invalid_line_never_becomes_an_object(quantity: int) -> None:
    order = make_order()
    with pytest.raises(ValidationError):
        order.add_line("SKU-9", Money.of("1.00"), quantity)
    assert len(order.lines) == 2  # the failed call left nothing behind


def test_an_address_without_a_postcode_is_rejected_on_construction() -> None:
    with pytest.raises(ValidationError):
        Address("1 Market St", "San Francisco", "   ")


def test_the_account_applies_its_own_rule_and_stays_consistent_when_it_refuses() -> None:
    account = LoyaltyAccount("C-1")
    account.earn(500)
    assert account.redeem(200) == Money.of("2.00")
    with pytest.raises(ConflictError):
        account.redeem(1_000)
    assert account.points == 300  # a refused redemption changes nothing


def test_checkout_delegates_to_policies_and_a_structural_fake_qualifies() -> None:
    class RecordingTax:
        """No base class, no registration: a matching ``tax_for`` is the whole interface."""

        def __init__(self) -> None:
            self.seen: list[str] = []

        def tax_for(self, subtotal: Money, postcode: str) -> Money:
            self.seen.append(postcode)
            return Money(0)

    repository = InMemoryOrderRepository()
    repository.save(make_order())
    tax = RecordingTax()
    service = CheckoutService(repository, tax, free_over(Money.of("100.00"), flat_shipping(Money.of("7.50"))))

    invoice = service.checkout("O-1")

    assert tax.seen == ["94107"]
    assert invoice.shipping == Money.of("7.50")  # 69.97 is under the free-shipping threshold
    assert invoice.total == Money.of("77.47")


def test_free_over_wraps_any_rule_including_another_wrapper() -> None:
    weight_based = flat_shipping(Money.of("12.00"))
    rule = free_over(Money.of("50.00"), free_over(Money.of("20.00"), weight_based))
    assert rule(Money.of("60.00")) == Money.of("0.00")
    assert rule(Money.of("25.00")) == Money.of("0.00")  # the inner wrapper decides
    assert rule(Money.of("10.00")) == Money.of("12.00")


def test_destination_tax_uses_the_first_matching_prefix() -> None:
    policy = DestinationTax(rates_by_prefix=(("941", 875), ("9", 600)), default_basis_points=0)
    assert policy.tax_for(Money.of("100.00"), "94107") == Money.of("8.75")
    assert policy.tax_for(Money.of("100.00"), "97201") == Money.of("6.00")
    assert policy.tax_for(Money.of("100.00"), "10001") == Money.of("0.00")


def test_the_order_refuses_the_second_checkout_and_the_repository_refuses_a_stranger() -> None:
    repository = InMemoryOrderRepository()
    repository.save(make_order())
    service = CheckoutService(repository, DestinationTax(), flat_shipping(Money(0)))

    service.checkout("O-1")

    with pytest.raises(InvalidStateError):
        service.checkout("O-1")
    with pytest.raises(NotFoundError):
        service.checkout("O-404")


def test_an_empty_order_cannot_be_placed() -> None:
    customer = Customer("C-2", "Grace", Address("2 Pine St", "Portland", "97201"))
    with pytest.raises(ValidationError):
        Order("O-2", customer).place()
