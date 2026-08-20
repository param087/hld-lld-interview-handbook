"""Adapter: the client is vendor-agnostic, the adapters translate both ways, the vendor never leaks."""

import pytest

from common import Money, NotFoundError, ValidationError
from patterns.adapter import (
    Checkout,
    LegacyTerminal,
    PaymentDeclinedError,
    PaymentProcessor,
    PaymentResult,
    PaymentStatus,
    PayPalAdapter,
    PayPalGateway,
    PayPalPayment,
    StripeAdapter,
    StripeClient,
    StripeError,
    terminal_charge,
)

AMOUNT = Money.of("12.34")


class RecordingStripe(StripeClient):
    """A vendor stub that remembers how it was called, so the translation can be asserted."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[int, str, str]] = []

    def create_charge(self, amount: int, currency: str, source: str) -> dict[str, object]:
        self.calls.append((amount, currency, source))
        return super().create_charge(amount, currency, source)


class RecordingPayPal(PayPalGateway):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, str]] = []

    def execute_payment(self, total: str, currency_code: str, payer_id: str) -> PayPalPayment:
        self.calls.append((total, currency_code, payer_id))
        return super().execute_payment(total, currency_code, payer_id)


@pytest.mark.parametrize(
    ("processor", "provider"),
    [(StripeAdapter(StripeClient()), "stripe"), (PayPalAdapter(PayPalGateway()), "paypal")],
    ids=["stripe", "paypal"],
)
def test_checkout_gets_the_same_result_shape_from_every_vendor(
    processor: PaymentProcessor, provider: str
) -> None:
    checkout = Checkout(processor)
    charged = checkout.pay(AMOUNT, "tok_visa")
    assert charged == PaymentResult(charged.payment_id, AMOUNT, PaymentStatus.CAPTURED, provider)
    refunded = checkout.refund(charged.payment_id)
    assert refunded == PaymentResult(charged.payment_id, AMOUNT, PaymentStatus.REFUNDED, provider)


def test_adapters_translate_units_names_and_case_for_each_vendor() -> None:
    stripe, paypal = RecordingStripe(), RecordingPayPal()
    StripeAdapter(stripe).charge(AMOUNT, "tok_visa")
    PayPalAdapter(paypal).charge(AMOUNT, "payer-7")
    assert stripe.calls == [(1234, "usd", "tok_visa")]  # integer cents, lower-case currency
    assert paypal.calls == [("12.34", "USD", "payer-7")]  # decimal string, upper-case currency


def test_vendor_failures_become_one_domain_error_and_the_vendor_type_stays_inside() -> None:
    with pytest.raises(PaymentDeclinedError) as stripe_failure:
        Checkout(StripeAdapter(StripeClient())).pay(AMOUNT, "tok_declined")
    assert isinstance(stripe_failure.value.__cause__, StripeError)  # chained for the logs
    with pytest.raises(PaymentDeclinedError, match="denied"):
        Checkout(PayPalAdapter(PayPalGateway())).pay(AMOUNT, "denied-payer")


def test_client_rules_run_before_any_vendor_call_and_a_fake_needs_no_base_class() -> None:
    class FakeProcessor:
        def __init__(self) -> None:
            self.calls = 0

        def charge(self, amount: Money, card_token: str) -> PaymentResult:
            self.calls += 1
            return PaymentResult("fake-1", amount, PaymentStatus.CAPTURED, "fake")

        def refund(self, payment_id: str) -> PaymentResult:
            self.calls += 1
            return PaymentResult(payment_id, Money(0), PaymentStatus.REFUNDED, "fake")

    fake = FakeProcessor()
    assert isinstance(fake, PaymentProcessor)  # structural: shape, not inheritance
    checkout = Checkout(fake)
    with pytest.raises(ValidationError):
        checkout.pay(Money(0), "tok_visa")
    with pytest.raises(NotFoundError):
        checkout.refund("never-charged")
    assert fake.calls == 0
    assert checkout.pay(Money(100), "tok_visa").payment_id == "fake-1"


def test_runtime_checkable_protocol_checks_method_names_not_signatures() -> None:
    class WrongShape:
        def charge(self) -> None: ...

        def refund(self) -> None: ...

    assert isinstance(WrongShape(), PaymentProcessor)  # passes: names only
    assert not isinstance(StripeClient(), PaymentProcessor)  # the raw vendor does not qualify


def test_closure_adapter_and_bound_method_both_serve_a_callable_target() -> None:
    charge = terminal_charge(LegacyTerminal())
    assert charge(Money.of("5.00"), "tok_visa") == PaymentResult(
        "AUTH1000", Money.of("5.00"), PaymentStatus.CAPTURED, "terminal"
    )
    with pytest.raises(PaymentDeclinedError):
        charge(Money.of("5.00"), "tok_declined")
    bound = StripeAdapter(StripeClient()).charge
    assert bound(Money.of("5.00"), "tok_visa").provider == "stripe"
