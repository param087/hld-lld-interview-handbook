"""Enums, value objects, entities and errors for the brokerage.

Money is ``common.Money`` (integer cents) everywhere; share counts are ints.
Behaviour that spans an account and an order lives in ``services.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"  # execute at whatever the feed says now
    LIMIT = "limit"  # execute only at the limit price or better


class OrderStatus(StrEnum):
    NEW = "new"  # validated, funds or shares reserved, not yet acknowledged
    SUBMITTED = "submitted"  # the exchange acknowledged it and it rests on the book
    PARTIALLY_FILLED = "partially_filled"  # some shares traded, the rest still resting
    FILLED = "filled"  # fully executed
    CANCELLED = "cancelled"  # pulled by the client, reservation released
    REJECTED = "rejected"  # the exchange refused it, reservation released


class AlertDirection(StrEnum):
    ABOVE = "above"
    BELOW = "below"


TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})

ORDER_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.SUBMITTED, OrderStatus.CANCELLED, OrderStatus.REJECTED}),
    OrderStatus.SUBMITTED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


# --8<-- [end:enums]


# --8<-- [start:errors]
class InsufficientFundsError(ConflictError):
    """Available cash (balance minus reservations) cannot cover the order."""


class InsufficientHoldingsError(ConflictError):
    """The account does not hold enough unreserved shares to sell."""


class OrderStateError(InvalidStateError):
    """The order is not in a state that allows this transition."""


class UnknownEntityError(NotFoundError):
    """No such account, order or symbol."""


# --8<-- [end:errors]


# --8<-- [start:market_values]
@dataclass(frozen=True, slots=True)
class Stock:
    symbol: str
    name: str


@dataclass(frozen=True, slots=True)
class Quote:
    """One price tick from the feed. Immutable, so a listener cannot rewrite history."""

    symbol: str
    price: Money
    at: float


@dataclass(frozen=True, slots=True)
class Fill:
    """An execution report from the exchange. ``fill_id`` is the idempotency key."""

    fill_id: str
    order_id: str
    quantity: int
    price: Money
    at: float


@dataclass(frozen=True, slots=True)
class Trade:
    """The settled result of one fill: what actually moved in the account."""

    id: str
    order_id: str
    account_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: Money
    notional: Money
    at: float


# --8<-- [end:market_values]


# --8<-- [start:account]
@dataclass(slots=True)
class Holding:
    """Shares of one symbol. ``reserved`` is what open sell orders have claimed."""

    symbol: str
    quantity: int = 0
    reserved: int = 0
    average_cost: Money = Money(0)

    def available(self) -> int:
        return self.quantity - self.reserved

    def reserve(self, quantity: int) -> None:
        if quantity > self.available():
            raise InsufficientHoldingsError(
                f"{self.symbol}: {self.available()} shares available, {quantity} requested"
            )
        self.reserved += quantity

    def release(self, quantity: int) -> None:
        self.reserved = max(0, self.reserved - quantity)

    def add(self, quantity: int, price: Money) -> None:
        """Weighted average cost; the rounding here is for display, never for cash."""
        total = self.average_cost * self.quantity + price * quantity
        self.quantity += quantity
        self.average_cost = Money(total.cents // self.quantity, price.currency)

    def remove(self, quantity: int) -> None:
        if quantity > self.quantity:
            raise InsufficientHoldingsError(f"{self.symbol}: cannot remove {quantity} of {self.quantity}")
        self.quantity -= quantity
        self.reserved = max(0, self.reserved - quantity)
        if self.quantity == 0:
            self.average_cost = Money(0, self.average_cost.currency)

    def copy(self) -> Holding:
        return Holding(self.symbol, self.quantity, self.reserved, self.average_cost)


@dataclass(slots=True)
class Portfolio:
    account_id: str
    holdings: dict[str, Holding] = field(default_factory=dict)

    def holding(self, symbol: str) -> Holding:
        return self.holdings.setdefault(symbol, Holding(symbol))

    def value(self, prices: Mapping[str, Money]) -> Money:
        total = Money(0)
        for symbol, holding in self.holdings.items():
            if symbol in prices:
                total = total + prices[symbol] * holding.quantity
        return total

    def copy(self) -> Portfolio:
        return Portfolio(self.account_id, {s: h.copy() for s, h in self.holdings.items()})


@dataclass(slots=True)
class Account:
    """Cash plus a portfolio. ``reserved_cash`` is what open buy orders have claimed."""

    id: str
    owner_id: str
    cash: Money
    portfolio: Portfolio
    reserved_cash: Money = Money(0)

    @classmethod
    def open(cls, account_id: str, owner_id: str, cash: Money) -> Account:
        return cls(account_id, owner_id, cash, Portfolio(account_id))

    def available_cash(self) -> Money:
        return self.cash - self.reserved_cash

    def reserve_cash(self, amount: Money) -> None:
        if amount > self.available_cash():
            raise InsufficientFundsError(f"{self.available_cash()} available, {amount} requested")
        self.reserved_cash = self.reserved_cash + amount

    def release_cash(self, amount: Money) -> None:
        self.reserved_cash = Money(max(0, (self.reserved_cash - amount).cents), amount.currency)

    def debit(self, amount: Money) -> None:
        if amount > self.cash:
            raise InsufficientFundsError(f"balance {self.cash} cannot cover {amount}")
        self.cash = self.cash - amount

    def credit(self, amount: Money) -> None:
        self.cash = self.cash + amount

    def copy(self) -> Account:
        return Account(self.id, self.owner_id, self.cash, self.portfolio.copy(), self.reserved_cash)


# --8<-- [end:account]


# --8<-- [start:watch]
@dataclass(slots=True)
class Watchlist:
    """A named set of symbols that caches the last price it heard. Observer of the feed."""

    id: str
    owner_id: str
    symbols: set[str] = field(default_factory=set)
    last_price: dict[str, Money] = field(default_factory=dict)

    def add(self, symbol: str) -> None:
        self.symbols.add(symbol)

    def on_quote(self, quote: Quote) -> None:
        if quote.symbol in self.symbols:
            self.last_price[quote.symbol] = quote.price

    def render(self) -> str:
        return ", ".join(f"{s} {self.last_price[s]}" for s in sorted(self.symbols) if s in self.last_price)


@dataclass(slots=True)
class PriceAlert:
    """Fires once when the price crosses the threshold, then stays triggered."""

    id: str
    owner_id: str
    symbol: str
    direction: AlertDirection
    threshold: Money
    triggered_at: float | None = None

    def matches(self, quote: Quote) -> bool:
        if self.triggered_at is not None or quote.symbol != self.symbol:
            return False
        if self.direction is AlertDirection.ABOVE:
            return quote.price >= self.threshold
        return quote.price <= self.threshold

    def trigger(self, at: float) -> None:
        if self.triggered_at is not None:
            raise ValidationError(f"alert {self.id} already fired")
        self.triggered_at = at


# --8<-- [end:watch]
