"""The price feed (Observer), the exchange gateway (Protocol + simulator) and alerts.

The feed knows nothing about orders; the exchange and the alert service are just
two listeners. That is what lets you add "trailing stop" or "push notification"
later without touching a line of the feed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.stock_brokerage.models import Fill, PriceAlert, Quote, UnknownEntityError
from lld.stock_brokerage.orders import Order


# --8<-- [start:feed]
class QuoteListener(Protocol):
    """Observer interface: anything that reacts to a price tick."""

    def on_quote(self, quote: Quote) -> None: ...


class MarketDataFeed:
    """Fans a tick out to every listener for that symbol, then to the wildcards.

    ``_lock`` guards the subscription map and the last-price cache only.
    Listeners are called *outside* it: a slow alert must not stall the feed, and
    a listener that publishes back into the feed must not deadlock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_symbol: dict[str, list[QuoteListener]] = {}
        self._all: list[QuoteListener] = []
        self._last: dict[str, Quote] = {}

    def subscribe(self, symbol: str, listener: QuoteListener) -> None:
        with self._lock:
            self._by_symbol.setdefault(symbol, []).append(listener)

    def subscribe_all(self, listener: QuoteListener) -> None:
        with self._lock:
            self._all.append(listener)

    def publish(self, quote: Quote) -> None:
        with self._lock:
            self._last[quote.symbol] = quote
            listeners = [*self._by_symbol.get(quote.symbol, ()), *self._all]
        for listener in listeners:
            listener.on_quote(quote)

    def last(self, symbol: str) -> Quote:
        with self._lock:
            try:
                return self._last[symbol]
            except KeyError:
                raise UnknownEntityError(f"no price yet for {symbol}") from None


# --8<-- [end:feed]


# --8<-- [start:alerts]
class AlertService:
    """Watches the feed and fires each alert once. A listener sees the crossing."""

    def __init__(self, clock: Clock | None = None, on_trigger: Callable[[PriceAlert], None] | None = None) -> None:
        self._clock = clock or SystemClock()
        self._on_trigger = on_trigger
        self._lock = threading.Lock()
        self._alerts: dict[str, PriceAlert] = {}

    def register(self, alert: PriceAlert) -> PriceAlert:
        with self._lock:
            self._alerts[alert.id] = alert
        return alert

    def on_quote(self, quote: Quote) -> None:
        now = self._clock.now()
        with self._lock:
            fired = [alert for alert in self._alerts.values() if alert.matches(quote)]
            for alert in fired:
                alert.trigger(now)
        if self._on_trigger is not None:
            for alert in fired:
                self._on_trigger(alert)

    def triggered(self) -> list[PriceAlert]:
        with self._lock:
            return [a for a in self._alerts.values() if a.triggered_at is not None]


# --8<-- [end:alerts]


# --8<-- [start:exchange]
class ExchangeGateway(Protocol):
    """The venue, as the broker sees it. Everything below this line is someone else's system."""

    def submit(self, order: Order) -> str: ...

    def cancel(self, exchange_order_id: str) -> bool: ...


@dataclass(slots=True)
class RestingOrder:
    """The venue's own copy of an order. It owns the remaining quantity, not the broker."""

    exchange_order_id: str
    order: Order
    remaining: int


class SimulatedExchange:
    """A stand-in venue: it rests orders and fills them when a tick makes them marketable.

    The venue keeps its own ``remaining`` counter -- the broker's ``Order`` object
    is replaced on every commit, so reading its state from here would be a bug.
    ``max_fill_quantity`` caps how many shares one tick can execute, which is how
    the tests and the demo reproduce partial fills deterministically. Fills are
    pushed to ``on_fill``: the broker's inbound execution report, in effect.
    """

    def __init__(
        self,
        on_fill: Callable[[Fill], None] | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        max_fill_quantity: int | None = None,
    ) -> None:
        self._on_fill = on_fill
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("F")
        self._max_fill = max_fill_quantity
        self._lock = threading.Lock()
        self._book: dict[str, RestingOrder] = {}

    def connect(self, on_fill: Callable[[Fill], None]) -> None:
        """Wire the broker's execution-report handler once both objects exist."""
        self._on_fill = on_fill

    def submit(self, order: Order) -> str:
        exchange_order_id = self._ids.next_id()
        with self._lock:
            self._book[exchange_order_id] = RestingOrder(exchange_order_id, order, order.remaining())
        return exchange_order_id

    def cancel(self, exchange_order_id: str) -> bool:
        with self._lock:
            return self._book.pop(exchange_order_id, None) is not None

    def on_quote(self, quote: Quote) -> None:
        """Fill one slice of every resting order this tick makes marketable, in submission order."""
        with self._lock:
            exchange_order_ids = list(self._book)
        for exchange_order_id in exchange_order_ids:
            fill = self._slice(exchange_order_id, quote)
            if fill is not None and self._on_fill is not None:
                self._on_fill(fill)  # outside the lock: the broker calls back into us

    def _slice(self, exchange_order_id: str, quote: Quote) -> Fill | None:
        with self._lock:
            resting = self._book.get(exchange_order_id)
            if resting is None or resting.order.symbol != quote.symbol:
                return None
            if not resting.order.is_marketable(quote) or resting.remaining <= 0:
                return None
            quantity = resting.remaining if self._max_fill is None else min(resting.remaining, self._max_fill)
            resting.remaining -= quantity
            if resting.remaining == 0:
                self._book.pop(exchange_order_id, None)
            price = resting.order.execution_price(quote)
            return Fill(self._ids.next_id(), resting.order.id, quantity, price, self._clock.now())

    def resting(self) -> Iterable[str]:
        with self._lock:
            return list(self._book)


# --8<-- [end:exchange]
