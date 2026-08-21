"""Discounts and bill splitting - the two rules a restaurant changes every quarter.

Both return ``Money``, and every split routes through ``Money.allocate`` so the shares
add up to the total to the cent. Rounding a split with ``total / n`` is the single most
common bug in this problem.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from common import Money, ValidationError
from lld.restaurant_management.models import Order


# --8<-- [start:discounts]
class DiscountPolicy(Protocol):
    """How much comes off the subtotal before tax."""

    def discount(self, order: Order, subtotal: Money) -> Money: ...


class NoDiscount:
    def discount(self, order: Order, subtotal: Money) -> Money:
        return Money(0, subtotal.currency)


class PercentageDiscount:
    """Staff meals, loyalty tiers, a manager comp."""

    def __init__(self, percent: Decimal) -> None:
        if not Decimal(0) <= percent <= Decimal(100):
            raise ValidationError("percent must be between 0 and 100")
        self._ratio = percent / Decimal(100)

    def discount(self, order: Order, subtotal: Money) -> Money:
        return subtotal * self._ratio


class LargePartyDiscount:
    """A flat amount off once the tab passes a threshold."""

    def __init__(self, threshold: Money, amount: Money) -> None:
        self._threshold = threshold
        self._amount = amount

    def discount(self, order: Order, subtotal: Money) -> Money:
        return self._amount if subtotal >= self._threshold else Money(0, subtotal.currency)


# --8<-- [end:discounts]


# --8<-- [start:splits]
class BillSplitStrategy(Protocol):
    """Turn one total into the shares the guests will actually pay."""

    def split(self, order: Order, total: Money) -> tuple[Money, ...]: ...


class NoSplit:
    def split(self, order: Order, total: Money) -> tuple[Money, ...]:
        return (total,)


class EvenSplit:
    """N ways. ``Money.allocate`` gives the odd cents to the first guests, deterministically."""

    def __init__(self, ways: int) -> None:
        if ways < 1:
            raise ValidationError("a bill splits at least one way")
        self._ways = ways

    def split(self, order: Order, total: Money) -> tuple[Money, ...]:
        return tuple(total.allocate([1] * self._ways))


class ByItemSplit:
    """Each guest pays for what they ordered; tax and discount ride along pro rata.

    The ratios are the guests' item subtotals in cents, so the discount and the tax
    are shared in the same proportion as the food - and the shares still sum exactly.
    """

    def __init__(self, groups: tuple[tuple[str, ...], ...]) -> None:
        if not groups:
            raise ValidationError("by-item split needs at least one guest group")
        self._groups = groups

    def split(self, order: Order, total: Money) -> tuple[Money, ...]:
        ratios: list[int] = []
        for group in self._groups:
            group_total = Money(0, total.currency)
            for item_id in group:
                group_total = group_total + order.item(item_id).line_total()
            ratios.append(group_total.cents)
        if sum(ratios) == 0:
            raise ValidationError("every guest group is empty; nothing to split")
        return tuple(total.allocate(ratios))


# --8<-- [end:splits]
