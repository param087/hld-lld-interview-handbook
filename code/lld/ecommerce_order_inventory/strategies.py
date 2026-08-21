"""Pluggable pricing rules, catalog specifications, and the payment gateway."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from decimal import Decimal
from typing import Protocol

from common import Money, ValidationError
from lld.ecommerce_order_inventory.models import (
    Address,
    OrderItem,
    ShippingSpeed,
    Sku,
)


def total_of(items: Iterable[OrderItem]) -> Money:
    total = Money(0)
    for item in items:
        total = total + item.line_total
    return total


# --8<-- [start:pricing]
class DiscountStrategy(Protocol):
    """How much comes off the basket. Implementations are stateless."""

    def discount(self, items: tuple[OrderItem, ...]) -> Money: ...


class NoDiscount:
    """Null Object: the default. Pricing never writes ``if discount is None``."""

    def discount(self, items: tuple[OrderItem, ...]) -> Money:
        return Money(0)


class PercentOff:
    """A capped percentage off the basket, above a minimum spend."""

    def __init__(self, percent: Decimal, cap: Money, min_subtotal: Money | None = None) -> None:
        if not 0 < percent < 1:
            raise ValidationError("percent must be a fraction between 0 and 1")
        self._percent = percent
        self._cap = cap
        self._min = min_subtotal or Money(0)

    def discount(self, items: tuple[OrderItem, ...]) -> Money:
        subtotal = total_of(items)
        if subtotal < self._min:
            return Money(0)
        return min(subtotal * self._percent, self._cap)


class CheapestFreeInBundle:
    """Buy N of anything, the cheapest one is free. Needs the lines, not the total."""

    def __init__(self, bundle_size: int = 3) -> None:
        self._size = bundle_size

    def discount(self, items: tuple[OrderItem, ...]) -> Money:
        units = sorted(item.unit_price for item in items for _ in range(item.quantity))
        free = len(units) // self._size
        total = Money(0)
        for price in units[:free]:
            total = total + price
        return total


class TaxCalculator(Protocol):
    def tax(self, taxable: Money, ship_to: Address) -> Money: ...


class ZeroTax:
    def tax(self, taxable: Money, ship_to: Address) -> Money:
        return Money(0)


class RegionTax:
    """One rate per region, with a default. Regions are strings from the address."""

    def __init__(self, rates: dict[str, Decimal], default: Decimal = Decimal("0.20")) -> None:
        self._rates = dict(rates)
        self._default = default

    def tax(self, taxable: Money, ship_to: Address) -> Money:
        return taxable * self._rates.get(ship_to.region, self._default)


class ShippingStrategy(Protocol):
    def cost(self, items: tuple[OrderItem, ...], ship_to: Address, speed: ShippingSpeed) -> Money: ...


class WeightBandShipping:
    """Flat fee per speed, free over a threshold. The rule every store starts with."""

    def __init__(
        self,
        standard: Money | None = None,
        express: Money | None = None,
        free_over: Money | None = None,
    ) -> None:
        self._standard = standard or Money.of("4.99")
        self._express = express or Money.of("12.99")
        self._free_over = free_over or Money.of("50.00")

    def cost(self, items: tuple[OrderItem, ...], ship_to: Address, speed: ShippingSpeed) -> Money:
        if speed is ShippingSpeed.EXPRESS:
            return self._express
        return Money(0) if total_of(items) >= self._free_over else self._standard


# --8<-- [end:pricing]


# --8<-- [start:specification]
class Specification(ABC):
    """A composable catalog predicate. ``InStock() & PriceBelow(...)`` reads as English.

    The point is not the operators; it is that search filters become objects you
    can name, unit-test and reuse in a query builder, instead of a growing pile
    of keyword arguments on ``search()``.
    """

    @abstractmethod
    def is_satisfied_by(self, sku: Sku, stock: int) -> bool: ...

    def __and__(self, other: Specification) -> Specification:
        return AndSpecification(self, other)

    def __or__(self, other: Specification) -> Specification:
        return OrSpecification(self, other)

    def __invert__(self) -> Specification:
        return NotSpecification(self)


class AndSpecification(Specification):
    def __init__(self, left: Specification, right: Specification) -> None:
        self._left, self._right = left, right

    def is_satisfied_by(self, sku: Sku, stock: int) -> bool:
        return self._left.is_satisfied_by(sku, stock) and self._right.is_satisfied_by(sku, stock)


class OrSpecification(Specification):
    def __init__(self, left: Specification, right: Specification) -> None:
        self._left, self._right = left, right

    def is_satisfied_by(self, sku: Sku, stock: int) -> bool:
        return self._left.is_satisfied_by(sku, stock) or self._right.is_satisfied_by(sku, stock)


class NotSpecification(Specification):
    def __init__(self, inner: Specification) -> None:
        self._inner = inner

    def is_satisfied_by(self, sku: Sku, stock: int) -> bool:
        return not self._inner.is_satisfied_by(sku, stock)


class InStock(Specification):
    def __init__(self, minimum: int = 1) -> None:
        self._minimum = minimum

    def is_satisfied_by(self, sku: Sku, stock: int) -> bool:
        return stock >= self._minimum


class PriceBelow(Specification):
    def __init__(self, ceiling: Money) -> None:
        self._ceiling = ceiling

    def is_satisfied_by(self, sku: Sku, stock: int) -> bool:
        return sku.price < self._ceiling


class HasAttribute(Specification):
    def __init__(self, key: str, value: str) -> None:
        self._pair = (key, value)

    def is_satisfied_by(self, sku: Sku, stock: int) -> bool:
        return self._pair in sku.attributes


# --8<-- [end:specification]


class PaymentGateway(Protocol):
    """Authorize now, capture when the parcel leaves. Two calls, on purpose."""

    def authorize(self, amount: Money, reference: str) -> bool: ...

    def capture(self, amount: Money, reference: str) -> bool: ...


class ApprovingGateway:
    def authorize(self, amount: Money, reference: str) -> bool:
        return True

    def capture(self, amount: Money, reference: str) -> bool:
        return True


class DecliningGateway:
    """Used in tests to prove that a declined card releases the held units."""

    def authorize(self, amount: Money, reference: str) -> bool:
        return False

    def capture(self, amount: Money, reference: str) -> bool:
        return False
