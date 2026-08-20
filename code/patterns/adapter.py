"""Adapter: make code you do not control fit the interface your code expects.

The running example is charging a card. ``Checkout`` (the client) is written
against one ``PaymentProcessor`` interface (the Target), in the domain's own
vocabulary: ``Money`` in, ``PaymentResult`` out, one domain error. Two vendor
SDK stand-ins (the Adaptees) disagree with it and with each other about method
names, amount units, result shapes and how failure is reported.
``StripeAdapter`` and ``PayPalAdapter`` translate each of them into the Target,
in both directions, and add no behaviour of their own.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from common import HandbookError, Money, NotFoundError, ValidationError


# --8<-- [start:target]
class PaymentStatus(StrEnum):
    CAPTURED = "captured"
    REFUNDED = "refunded"


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """What the client needs to know, in the client's vocabulary."""

    payment_id: str
    amount: Money
    status: PaymentStatus
    provider: str


class PaymentDeclinedError(HandbookError):
    """The one failure the client handles, whatever the vendor raised or returned."""


@runtime_checkable
class PaymentProcessor(Protocol):
    """The Target: the interface the client is written against.

    It belongs to the domain, not to any vendor: amounts are ``Money``, failures
    are ``PaymentDeclinedError`` and results are ``PaymentResult``.
    """

    def charge(self, amount: Money, card_token: str) -> PaymentResult: ...

    def refund(self, payment_id: str) -> PaymentResult: ...


# --8<-- [end:target]


# --8<-- [start:adaptees]
# Stand-ins for two vendor SDKs. You do not own this code and cannot rename its methods.
class StripeError(Exception):
    """The vendor's exception type. It must never leak out of the adapter."""


class StripeClient:
    """Integer minor units, lower-case currency, plain dicts back, failures raised."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._charges: dict[str, dict[str, object]] = {}

    def create_charge(self, amount: int, currency: str, source: str) -> dict[str, object]:
        if source == "tok_declined":
            raise StripeError("card_declined")
        charge: dict[str, object] = {
            "id": f"ch_{next(self._ids)}",
            "amount": amount,
            "currency": currency,
            "status": "succeeded",
        }
        self._charges[str(charge["id"])] = charge
        return charge

    def create_refund(self, charge: str) -> dict[str, object]:
        original = self._charges[charge]
        return {
            "id": f"re_{next(self._ids)}",
            "charge": charge,
            "amount": original["amount"],
            "currency": original["currency"],
            "status": "succeeded",
        }


@dataclass(frozen=True, slots=True)
class PayPalPayment:
    payment_id: str
    state: str
    total: str
    currency_code: str


class PayPalGateway:
    """Decimal strings, upper-case currency, objects back, failures reported in a field."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._payments: dict[str, PayPalPayment] = {}

    def execute_payment(self, total: str, currency_code: str, payer_id: str) -> PayPalPayment:
        state = "denied" if payer_id.startswith("denied") else "approved"
        payment = PayPalPayment(f"PAY-{next(self._ids)}", state, total, currency_code)
        self._payments[payment.payment_id] = payment
        return payment

    def refund_sale(self, payment_id: str) -> PayPalPayment:
        original = self._payments[payment_id]
        return PayPalPayment(payment_id, "refunded", original.total, original.currency_code)


# --8<-- [end:adaptees]


# --8<-- [start:adapters]
class StripeAdapter:
    """An object adapter: holds the vendor client and translates every call both ways.

    Arguments: ``Money`` becomes integer cents plus a lower-case currency.
    Results: the vendor dict becomes a ``PaymentResult``.
    Errors: ``StripeError`` becomes ``PaymentDeclinedError`` (chained, for the logs).
    """

    provider = "stripe"

    def __init__(self, client: StripeClient) -> None:
        self._client = client

    def charge(self, amount: Money, card_token: str) -> PaymentResult:
        try:
            raw = self._client.create_charge(amount.cents, amount.currency.lower(), card_token)
        except StripeError as exc:
            raise PaymentDeclinedError(f"stripe: {exc}") from exc
        return PaymentResult(str(raw["id"]), amount, PaymentStatus.CAPTURED, self.provider)

    def refund(self, payment_id: str) -> PaymentResult:
        raw = self._client.create_refund(payment_id)
        amount = Money(int(str(raw["amount"])), str(raw["currency"]).upper())
        return PaymentResult(payment_id, amount, PaymentStatus.REFUNDED, self.provider)


class PayPalAdapter:
    """Same Target, different translation: decimal strings and a status field to inspect."""

    provider = "paypal"

    def __init__(self, gateway: PayPalGateway) -> None:
        self._gateway = gateway

    def charge(self, amount: Money, card_token: str) -> PaymentResult:
        payment = self._gateway.execute_payment(_decimal_string(amount), amount.currency, card_token)
        if payment.state != "approved":
            raise PaymentDeclinedError(f"paypal: payment {payment.state}")
        return PaymentResult(payment.payment_id, amount, PaymentStatus.CAPTURED, self.provider)

    def refund(self, payment_id: str) -> PaymentResult:
        refund = self._gateway.refund_sale(payment_id)
        amount = Money.of(refund.total, refund.currency_code)
        return PaymentResult(payment_id, amount, PaymentStatus.REFUNDED, self.provider)


def _decimal_string(amount: Money) -> str:
    """``Money(1234)`` -> ``"12.34"`` without going through a float."""
    units, cents = divmod(amount.cents, 100)
    return f"{units}.{cents:02d}"


# --8<-- [end:adapters]


# --8<-- [start:client]
class Checkout:
    """The client: written against the Target only. It never imports a vendor type.

    Domain rules live here (positive amounts, refund only what you charged);
    vendor translation lives in the adapters. Neither knows the other's job.
    """

    def __init__(self, processor: PaymentProcessor) -> None:
        self._processor = processor
        self._payments: dict[str, PaymentResult] = {}

    def pay(self, amount: Money, card_token: str) -> PaymentResult:
        if amount.cents <= 0:
            raise ValidationError("amount must be positive")
        result = self._processor.charge(amount, card_token)
        self._payments[result.payment_id] = result
        return result

    def refund(self, payment_id: str) -> PaymentResult:
        if payment_id not in self._payments:
            raise NotFoundError(f"unknown payment {payment_id}")
        result = self._processor.refund(payment_id)
        self._payments[payment_id] = result
        return result


# --8<-- [end:client]


# --8<-- [start:pythonic]
# When the client needs one call, the Target is a Callable and the adapter is a closure.
type ChargeFn = Callable[[Money, str], PaymentResult]


class LegacyTerminal:
    """A third adaptee that can only charge: integer cents in, an authorisation code out."""

    def __init__(self) -> None:
        self._codes = itertools.count(1000)

    def swipe(self, cents: int, card: str) -> str:
        if card == "tok_declined":
            return "DECLINED"
        return f"AUTH{next(self._codes)}"


def terminal_charge(terminal: LegacyTerminal) -> ChargeFn:
    """Adapt to the smallest interface the caller needs; a terminal cannot refund, so it is not forced to pretend."""

    def charge(amount: Money, card_token: str) -> PaymentResult:
        code = terminal.swipe(amount.cents, card_token)
        if code == "DECLINED":
            raise PaymentDeclinedError("terminal: declined")
        return PaymentResult(code, amount, PaymentStatus.CAPTURED, "terminal")

    return charge


# --8<-- [end:pythonic]


def main() -> None:
    amount = Money.of("12.34")
    processors: list[PaymentProcessor] = [StripeAdapter(StripeClient()), PayPalAdapter(PayPalGateway())]

    print("--- one Checkout, two vendors, one vocabulary ---")
    receipts: list[tuple[Checkout, PaymentResult]] = []
    for processor in processors:
        checkout = Checkout(processor)
        result = checkout.pay(amount, "tok_visa")
        receipts.append((checkout, result))
        print(f"{result.provider:>6}: charged {result.amount} -> {result.payment_id} ({result.status})")

    print("--- refunds travel through the same adapter ---")
    for checkout, result in receipts:
        refund = checkout.refund(result.payment_id)
        print(f"{refund.provider:>6}: refunded {refund.amount} for {refund.payment_id} ({refund.status})")

    print("--- every vendor failure arrives as one domain error ---")
    for processor, token in zip(processors, ["tok_declined", "denied-payer"], strict=True):
        try:
            Checkout(processor).pay(amount, token)
        except PaymentDeclinedError as exc:
            print(f"declined: {exc}")

    print("--- callable targets: a closure adapter and a bound method, side by side ---")
    charge_fns: list[ChargeFn] = [terminal_charge(LegacyTerminal()), StripeAdapter(StripeClient()).charge]
    for charge in charge_fns:
        result = charge(Money.of("5.00"), "tok_visa")
        print(f"{result.provider:>8}: {result.amount} -> {result.payment_id}")

    try:
        Checkout(processors[0]).pay(Money(0), "tok_visa")
    except ValidationError as exc:
        print(f"rejected before any vendor call: {exc}")


if __name__ == "__main__":
    main()
