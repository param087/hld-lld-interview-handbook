"""Pluggable policies: courier ranking, coupons, and payment gateways."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from common import Money, ValidationError
from lld.food_delivery.models import (
    DeliveryPartner,
    Location,
    PaymentMethod,
)


# --8<-- [start:assignment]
class AssignmentStrategy(Protocol):
    """Ranks candidate couriers for one order, best first.

    A strategy only *ranks*. It never mutates a courier and never takes a lock --
    ``DeliveryService`` does both, which is why swapping the ranking rule cannot
    introduce a race.
    """

    def rank(self, origin: Location, candidates: Sequence[DeliveryPartner]) -> list[DeliveryPartner]: ...


class NearestPartner:
    """Straight-line distance from the restaurant. The obvious first answer."""

    def rank(self, origin: Location, candidates: Sequence[DeliveryPartner]) -> list[DeliveryPartner]:
        return sorted(candidates, key=lambda p: (origin.distance_km(p.location), p.id))


class BestRatedNearby:
    """Everyone inside the radius, best rated first. Distance only breaks ties."""

    def __init__(self, radius_km: float = 3.0) -> None:
        self._radius = radius_km

    def rank(self, origin: Location, candidates: Sequence[DeliveryPartner]) -> list[DeliveryPartner]:
        nearby = [p for p in candidates if origin.distance_km(p.location) <= self._radius]
        return sorted(nearby, key=lambda p: (-p.rating, origin.distance_km(p.location), p.id))


class FairRotation:
    """Fewest deliveries today first, then distance. Keeps earnings even."""

    def __init__(self, radius_km: float = 5.0) -> None:
        self._radius = radius_km

    def rank(self, origin: Location, candidates: Sequence[DeliveryPartner]) -> list[DeliveryPartner]:
        nearby = [p for p in candidates if origin.distance_km(p.location) <= self._radius]
        return sorted(nearby, key=lambda p: (p.deliveries_today, origin.distance_km(p.location), p.id))


# --8<-- [end:assignment]


# --8<-- [start:discount]
class DiscountStrategy(Protocol):
    """How much comes off this order. Implementations are stateless."""

    def discount(self, subtotal: Money, delivery_fee: Money) -> Money: ...


class NoDiscount:
    """Null Object: the default coupon. Callers never branch on ``coupon is None``."""

    def discount(self, subtotal: Money, delivery_fee: Money) -> Money:
        return Money(0)


class FlatOff:
    """A fixed amount off, above a minimum spend."""

    def __init__(self, amount: Money, min_subtotal: Money) -> None:
        self._amount = amount
        self._min = min_subtotal

    def discount(self, subtotal: Money, delivery_fee: Money) -> Money:
        if subtotal < self._min:
            return Money(0)
        return min(self._amount, subtotal)


class PercentOff:
    """A percentage off the food, capped so a large order cannot drain the budget."""

    def __init__(self, percent: Decimal, cap: Money) -> None:
        if not 0 < percent < 1:
            raise ValidationError("percent must be a fraction between 0 and 1")
        self._percent = percent
        self._cap = cap

    def discount(self, subtotal: Money, delivery_fee: Money) -> Money:
        return min(subtotal * self._percent, self._cap)


class FreeDelivery:
    """Takes the delivery fee off instead of the food."""

    def discount(self, subtotal: Money, delivery_fee: Money) -> Money:
        return delivery_fee


class CouponBook:
    """Code to strategy. An unknown code is an error; *no* code is a Null Object."""

    def __init__(self, coupons: dict[str, DiscountStrategy] | None = None) -> None:
        self._coupons: dict[str, DiscountStrategy] = dict(coupons or {})

    def register(self, code: str, strategy: DiscountStrategy) -> None:
        self._coupons[code.upper()] = strategy

    def lookup(self, code: str | None) -> DiscountStrategy:
        if code is None:
            return NoDiscount()
        try:
            return self._coupons[code.upper()]
        except KeyError:
            raise ValidationError(f"unknown coupon {code!r}") from None


# --8<-- [end:discount]


class PaymentGateway(Protocol):
    """Authorize now, capture later. Two calls, because food can fail to arrive."""

    def authorize(self, amount: Money) -> bool: ...

    def capture(self, amount: Money) -> bool: ...


class ApprovingGateway:
    def authorize(self, amount: Money) -> bool:
        return True

    def capture(self, amount: Money) -> bool:
        return True


class CashOnDeliveryGateway:
    """Nothing to authorize; the courier collects. Capture always succeeds."""

    def authorize(self, amount: Money) -> bool:
        return True

    def capture(self, amount: Money) -> bool:
        return True


class GatewayFactory:
    """Factory: the checkout call carries a method string, not a gateway object."""

    _registry: dict[PaymentMethod, type] = {
        PaymentMethod.CARD: ApprovingGateway,
        PaymentMethod.WALLET: ApprovingGateway,
        PaymentMethod.CASH: CashOnDeliveryGateway,
    }

    @classmethod
    def for_method(cls, method: PaymentMethod | str) -> PaymentGateway:
        try:
            return cls._registry[PaymentMethod(method)]()
        except ValueError as exc:
            raise ValidationError(f"unknown payment method: {method!r}") from exc
