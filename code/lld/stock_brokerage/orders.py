"""The order hierarchy: one base entity, two behaviours, one factory.

``MarketOrder`` and ``LimitOrder`` differ in exactly two questions the exchange
asks -- "would you trade at this price?" and "at what price?" -- so they are two
overridden methods, not two branches inside the service.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import ClassVar

from common import Money, ValidationError
from lld.stock_brokerage.models import (
    ORDER_TRANSITIONS,
    OrderSide,
    OrderStateError,
    OrderStatus,
    OrderType,
    Quote,
)


# --8<-- [start:orders]
@dataclass(slots=True, kw_only=True)
class Order:
    """An intention to trade, plus everything needed to unwind its reservation.

    ``unit_reserve`` is the cash reserved per share for a buy; releasing a
    partial fill is then ``unit_reserve * filled_shares``, which is exact in
    cents and needs no division.
    """

    id: str
    account_id: str
    symbol: str
    side: OrderSide
    quantity: int
    created_at: float
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: int = 0
    filled_notional: Money = Money(0)
    unit_reserve: Money = Money(0)
    exchange_order_id: str | None = None

    order_type: ClassVar[OrderType]

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValidationError("order quantity must be positive")

    def remaining(self) -> int:
        return self.quantity - self.filled_quantity

    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)

    def is_marketable(self, quote: Quote) -> bool:
        raise NotImplementedError

    def execution_price(self, quote: Quote) -> Money:
        raise NotImplementedError

    def reference_price(self, quote: Quote) -> Money:
        """The price used to size the cash reservation before anything trades."""
        raise NotImplementedError

    def average_price(self) -> Money:
        if self.filled_quantity == 0:
            return Money(0, self.filled_notional.currency)
        cents = self.filled_notional.cents
        return Money((cents + self.filled_quantity // 2) // self.filled_quantity, self.filled_notional.currency)

    def transition_to(self, status: OrderStatus) -> None:
        if status not in ORDER_TRANSITIONS[self.status]:
            raise OrderStateError(f"order {self.id}: {self.status} cannot become {status}")
        self.status = status

    def apply_fill(self, quantity: int, price: Money) -> None:
        """Book shares against the order and move it to PARTIALLY_FILLED or FILLED."""
        if quantity <= 0 or quantity > self.remaining():
            raise ValidationError(f"order {self.id}: cannot fill {quantity} of {self.remaining()} remaining")
        self.filled_quantity += quantity
        self.filled_notional = self.filled_notional + price * quantity
        self.transition_to(OrderStatus.FILLED if self.remaining() == 0 else OrderStatus.PARTIALLY_FILLED)

    def copy(self) -> Order:
        return type(self)(**{f.name: getattr(self, f.name) for f in fields(self)})


# 5% headroom: a market buy reserves slightly more than the last print, because
# the price can move between the reservation and the fill.
MARKET_RESERVE_BASIS_POINTS = 10_500


@dataclass(slots=True, kw_only=True)
class MarketOrder(Order):
    """Trades at whatever the feed prints next. Always marketable."""

    order_type: ClassVar[OrderType] = OrderType.MARKET

    def is_marketable(self, quote: Quote) -> bool:
        return True

    def execution_price(self, quote: Quote) -> Money:
        return quote.price

    def reference_price(self, quote: Quote) -> Money:
        return Money(quote.price.cents * MARKET_RESERVE_BASIS_POINTS // 10_000, quote.price.currency)


@dataclass(slots=True, kw_only=True)
class LimitOrder(Order):
    """Trades only at the limit or better, so the reservation is exact."""

    order_type: ClassVar[OrderType] = OrderType.LIMIT
    limit_price: Money

    def __post_init__(self) -> None:
        # Named base call, not ``super()``: ``slots=True`` rebuilds the class object
        # and the zero-argument ``super()`` cell would still point at the old one.
        Order.__post_init__(self)
        if self.limit_price.cents <= 0:
            raise ValidationError("limit price must be positive")

    def is_marketable(self, quote: Quote) -> bool:
        if self.side is OrderSide.BUY:
            return quote.price <= self.limit_price
        return quote.price >= self.limit_price

    def execution_price(self, quote: Quote) -> Money:
        if self.side is OrderSide.BUY:
            return min(quote.price, self.limit_price)
        return max(quote.price, self.limit_price)

    def reference_price(self, quote: Quote) -> Money:
        return self.limit_price


class OrderFactory:
    """Factory Method: the API sends ``"limit"`` plus a price, the registry picks the class."""

    @staticmethod
    def create(
        order_type: OrderType | str,
        *,
        order_id: str,
        account_id: str,
        symbol: str,
        side: OrderSide,
        quantity: int,
        created_at: float,
        limit_price: Money | None = None,
    ) -> Order:
        kind = OrderType(order_type)
        common = {
            "id": order_id,
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "created_at": created_at,
        }
        if kind is OrderType.MARKET:
            if limit_price is not None:
                raise ValidationError("a market order cannot carry a limit price")
            return MarketOrder(**common)
        if limit_price is None:
            raise ValidationError("a limit order needs a limit price")
        return LimitOrder(**common, limit_price=limit_price)


# --8<-- [end:orders]
