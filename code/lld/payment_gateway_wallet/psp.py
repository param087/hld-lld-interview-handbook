"""Payment service providers: three vendor clients with three shapes, one interface.

The clients below are deliberately inconsistent -- a dict, a tuple, a status
string -- because that is what real vendor SDKs look like. Each adapter converts
one of them into ``PspResult``, and ``PaymentProcessorFactory`` picks the adapter
from the payment method type. Nothing above this file knows a vendor exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from common import Money, ValidationError
from lld.payment_gateway_wallet.models import PaymentMethod, PaymentMethodType


# --8<-- [start:psp]
@dataclass(frozen=True, slots=True)
class PspResult:
    """The single shape the gateway understands, whatever the vendor returned."""

    approved: bool
    reference: str
    code: str

    @classmethod
    def declined(cls, code: str) -> PspResult:
        return cls(False, "", code)


class PaymentProcessor(Protocol):
    """Authorize, capture, refund. Everything below this line is someone else's system."""

    def authorize(self, method: PaymentMethod, amount: Money, reference: str) -> PspResult: ...

    def capture(self, authorization_id: str, amount: Money) -> PspResult: ...

    def refund(self, capture_id: str, amount: Money) -> PspResult: ...


class CardNetworkClient:
    """Vendor A: returns dictionaries and thinks in minor units."""

    def __init__(self, declined_tokens: Iterable[str] = ()) -> None:
        self._declined = frozenset(declined_tokens)
        self._counter = 0

    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:04d}"

    def authorize_charge(self, card_token: str, minor_units: int, currency: str) -> dict[str, str]:
        if card_token in self._declined:
            return {"status": "declined", "decline_code": "do_not_honor"}
        return {"status": "authorized", "auth_id": self._next("auth"), "currency": currency}

    def capture_charge(self, auth_id: str, minor_units: int) -> dict[str, str]:
        return {"status": "captured", "charge_id": self._next("ch")}

    def refund_charge(self, charge_id: str, minor_units: int) -> dict[str, str]:
        return {"status": "refunded", "refund_id": self._next("re")}


class UpiSwitchClient:
    """Vendor B: returns ``(ok, reference)`` tuples and calls the reference an RRN."""

    def __init__(self, declined_vpas: Iterable[str] = ()) -> None:
        self._declined = frozenset(declined_vpas)
        self._counter = 0

    def collect(self, vpa: str, paise: int) -> tuple[bool, str]:
        if vpa in self._declined:
            return False, "USER_DECLINED"
        self._counter += 1
        return True, f"RRN{self._counter:06d}"

    def reverse(self, rrn: str, paise: int) -> tuple[bool, str]:
        return True, f"REV{rrn}"


class NetbankingClient:
    """Vendor C: one method, a colon-separated status string, no capture step."""

    def __init__(self, declined_accounts: Iterable[str] = ()) -> None:
        self._declined = frozenset(declined_accounts)
        self._counter = 0

    def initiate(self, account_ref: str, amount_cents: int) -> str:
        if account_ref in self._declined:
            return "FAILED:INSUFFICIENT_FUNDS"
        self._counter += 1
        return f"OK:NB{self._counter:06d}"

    def initiate_refund(self, bank_ref: str, amount_cents: int) -> str:
        return f"OK:RF{bank_ref}"


class CardProcessorAdapter:
    """Adapter: two-step card flow (authorize then capture) behind ``PaymentProcessor``."""

    def __init__(self, client: CardNetworkClient) -> None:
        self._client = client

    def authorize(self, method: PaymentMethod, amount: Money, reference: str) -> PspResult:
        response = self._client.authorize_charge(method.token, amount.cents, amount.currency)
        if response["status"] != "authorized":
            return PspResult.declined(response["decline_code"])
        return PspResult(True, response["auth_id"], "approved")

    def capture(self, authorization_id: str, amount: Money) -> PspResult:
        response = self._client.capture_charge(authorization_id, amount.cents)
        return PspResult(True, response["charge_id"], "captured")

    def refund(self, capture_id: str, amount: Money) -> PspResult:
        response = self._client.refund_charge(capture_id, amount.cents)
        return PspResult(True, response["refund_id"], "refunded")


class UpiProcessorAdapter:
    """Adapter: UPI authorizes and captures in one collect call, so capture is a no-op."""

    def __init__(self, client: UpiSwitchClient) -> None:
        self._client = client

    def authorize(self, method: PaymentMethod, amount: Money, reference: str) -> PspResult:
        ok, value = self._client.collect(method.token, amount.cents)
        return PspResult(True, value, "approved") if ok else PspResult.declined(value)

    def capture(self, authorization_id: str, amount: Money) -> PspResult:
        return PspResult(True, authorization_id, "captured")

    def refund(self, capture_id: str, amount: Money) -> PspResult:
        ok, value = self._client.reverse(capture_id, amount.cents)
        return PspResult(ok, value, "refunded" if ok else "reversal_failed")


class NetbankingProcessorAdapter:
    """Adapter: parses the vendor's status string into the shared result type."""

    def __init__(self, client: NetbankingClient) -> None:
        self._client = client

    @staticmethod
    def _parse(raw: str) -> PspResult:
        status, _, value = raw.partition(":")
        return PspResult(True, value, "approved") if status == "OK" else PspResult.declined(value)

    def authorize(self, method: PaymentMethod, amount: Money, reference: str) -> PspResult:
        return self._parse(self._client.initiate(method.token, amount.cents))

    def capture(self, authorization_id: str, amount: Money) -> PspResult:
        return PspResult(True, authorization_id, "captured")

    def refund(self, capture_id: str, amount: Money) -> PspResult:
        return self._parse(self._client.initiate_refund(capture_id, amount.cents))


class PaymentProcessorFactory:
    """Maps a payment method type to the adapter that speaks its rail."""

    def __init__(self, processors: dict[PaymentMethodType, PaymentProcessor]) -> None:
        self._processors = dict(processors)

    @classmethod
    def with_stubs(cls, declined_tokens: Iterable[str] = ()) -> PaymentProcessorFactory:
        tokens = frozenset(declined_tokens)
        return cls(
            {
                PaymentMethodType.CARD: CardProcessorAdapter(CardNetworkClient(tokens)),
                PaymentMethodType.UPI: UpiProcessorAdapter(UpiSwitchClient(tokens)),
                PaymentMethodType.NETBANKING: NetbankingProcessorAdapter(NetbankingClient(tokens)),
            }
        )

    def for_method(self, method: PaymentMethod) -> PaymentProcessor:
        try:
            return self._processors[method.type]
        except KeyError:
            raise ValidationError(f"no processor configured for {method.type}") from None


# --8<-- [end:psp]
