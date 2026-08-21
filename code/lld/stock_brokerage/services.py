"""``OrderService``: the facade the app talks to, and the only writer of money.

Read ``place_order`` and ``on_fill`` together. Placement reserves first and asks
the venue second; settlement is idempotent on the fill id, so an execution
report delivered twice moves the account exactly once.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Protocol

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock, ValidationError
from lld.stock_brokerage.market import ExchangeGateway, MarketDataFeed
from lld.stock_brokerage.models import (
    TERMINAL_STATUSES,
    Account,
    Fill,
    OrderSide,
    OrderStateError,
    OrderStatus,
    OrderType,
    Trade,
    UnknownEntityError,
)
from lld.stock_brokerage.orders import Order, OrderFactory
from lld.stock_brokerage.store import AccountState, AccountUnitOfWork, BrokerageStore


# --8<-- [start:listener]
class TradeListener(Protocol):
    """Observer of settled trades: statements, push notifications, risk checks."""

    def on_trade(self, trade: Trade) -> None: ...


class TradeLog:
    """The simplest listener: an append-only list, safe to read from another thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trades: list[Trade] = []

    def on_trade(self, trade: Trade) -> None:
        with self._lock:
            self._trades.append(trade)

    def all(self) -> list[Trade]:
        with self._lock:
            return list(self._trades)


# --8<-- [end:listener]


# --8<-- [start:service]
class OrderService:
    """Place, cancel and settle. Every method that moves money takes the account lock."""

    def __init__(
        self,
        store: BrokerageStore,
        feed: MarketDataFeed,
        exchange: ExchangeGateway,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        listeners: Iterable[TradeListener] = (),
    ) -> None:
        self._store = store
        self._feed = feed
        self._exchange = exchange
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("O")
        self._listeners = list(listeners)

    def place_order(
        self,
        account_id: str,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType | str = OrderType.MARKET,
        limit_price: Money | None = None,
    ) -> Order:
        """Reserve funds or shares, persist, then hand the order to the venue.

        The exchange call happens *after* the transaction commits and *outside*
        the account lock: never hold a lock across a network call. The cost is
        that a fill can arrive before the acknowledgement lands, which
        ``_acknowledge`` handles by leaving an already-progressed order alone.
        """
        self._store.stock(symbol)
        quote = self._feed.last(symbol)
        with self._store.account_lock(account_id):
            with AccountUnitOfWork(self._store, account_id) as uow:
                order = OrderFactory.create(
                    order_type,
                    order_id=self._ids.next_id(),
                    account_id=account_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    created_at=self._clock.now(),
                    limit_price=limit_price,
                )
                self._reserve(uow.state, order, quote_price=order.reference_price(quote))
                uow.state.orders[order.id] = order
                uow.commit()
        self._store.index_order(order.id, account_id)
        try:
            exchange_order_id = self._exchange.submit(order)
        except Exception:
            self._finish(account_id, order.id, OrderStatus.REJECTED)
            raise
        return self._acknowledge(account_id, order.id, exchange_order_id)

    def cancel_order(self, account_id: str, order_id: str) -> Order:
        """Release whatever is still reserved, then pull the order off the book."""
        order = self._finish(account_id, order_id, OrderStatus.CANCELLED)
        if order.exchange_order_id is not None:
            self._exchange.cancel(order.exchange_order_id)
        return order

    def on_fill(self, fill: Fill) -> Trade:
        """Settle one execution report. Idempotent: a repeated fill id returns the first trade."""
        account_id = self._store.account_for_order(fill.order_id)
        with self._store.account_lock(account_id):
            with AccountUnitOfWork(self._store, account_id) as uow:
                seen = uow.state.trades_by_fill.get(fill.fill_id)
                if seen is not None:
                    return seen  # duplicate delivery: no second debit, no second trade
                order = uow.state.orders[fill.order_id]
                if order.status in TERMINAL_STATUSES:
                    raise OrderStateError(f"order {order.id} is {order.status}; fill {fill.fill_id} arrived too late")
                trade = self._settle(uow.state, order, fill)
                uow.state.trades.append(trade)
                uow.state.trades_by_fill[fill.fill_id] = trade
                uow.commit()
        for listener in self._listeners:
            listener.on_trade(trade)  # outside the lock
        return trade

    # -- queries ---------------------------------------------------------------------
    def account(self, account_id: str) -> Account:
        return self._store.snapshot(account_id).account

    def order(self, account_id: str, order_id: str) -> Order:
        try:
            return self._store.snapshot(account_id).orders[order_id]
        except KeyError:
            raise UnknownEntityError(f"unknown order {order_id}") from None

    def trades(self, account_id: str) -> list[Trade]:
        return self._store.snapshot(account_id).trades

    def open_orders(self, account_id: str) -> list[Order]:
        return [o for o in self._store.snapshot(account_id).orders.values() if o.is_open()]

    # -- internals -------------------------------------------------------------------
    @staticmethod
    def _reserve(state: AccountState, order: Order, quote_price: Money) -> None:
        """Buy: hold cash at the reference price. Sell: hold the shares themselves."""
        if order.side is OrderSide.BUY:
            order.unit_reserve = quote_price
            state.account.reserve_cash(quote_price * order.quantity)
        else:
            state.account.portfolio.holding(order.symbol).reserve(order.quantity)

    def _settle(self, state: AccountState, order: Order, fill: Fill) -> Trade:
        notional = fill.price * fill.quantity
        holding = state.account.portfolio.holding(order.symbol)
        if order.side is OrderSide.BUY:
            state.account.release_cash(order.unit_reserve * fill.quantity)
            state.account.debit(notional)
            holding.add(fill.quantity, fill.price)
        else:
            holding.remove(fill.quantity)
            state.account.credit(notional)
        order.apply_fill(fill.quantity, fill.price)
        if order.status is OrderStatus.FILLED:
            self._release_remainder(state, order)
        return Trade(
            id=self._ids.next_id(),
            order_id=order.id,
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill.quantity,
            price=fill.price,
            notional=notional,
            at=fill.at,
        )

    @staticmethod
    def _release_remainder(state: AccountState, order: Order) -> None:
        """Give back what the unfilled part of the order was still holding."""
        if order.side is OrderSide.BUY:
            state.account.release_cash(order.unit_reserve * order.remaining())
        else:
            state.account.portfolio.holding(order.symbol).release(order.remaining())

    def _acknowledge(self, account_id: str, order_id: str, exchange_order_id: str) -> Order:
        with self._store.account_lock(account_id):
            with AccountUnitOfWork(self._store, account_id) as uow:
                order = uow.state.orders[order_id]
                order.exchange_order_id = exchange_order_id
                if order.status is OrderStatus.NEW:
                    order.transition_to(OrderStatus.SUBMITTED)  # a fill may already have moved it on
                uow.commit()
                return order

    def _finish(self, account_id: str, order_id: str, status: OrderStatus) -> Order:
        with self._store.account_lock(account_id):
            with AccountUnitOfWork(self._store, account_id) as uow:
                order = uow.state.orders.get(order_id)
                if order is None:
                    raise UnknownEntityError(f"unknown order {order_id}")
                if not order.is_open():
                    raise OrderStateError(f"order {order_id} is already {order.status}")
                self._release_remainder(uow.state, order)
                order.transition_to(status)
                uow.commit()
                return order


# --8<-- [end:service]


# --8<-- [start:commands]
class OrderCommand(Protocol):
    """A trading request as an object, so the app can queue, log and undo it."""

    def execute(self) -> Order: ...

    def undo(self) -> Order: ...


class PlaceOrderCommand:
    """Places an order on ``execute`` and cancels that same order on ``undo``."""

    def __init__(
        self,
        service: OrderService,
        account_id: str,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType | str = OrderType.MARKET,
        limit_price: Money | None = None,
    ) -> None:
        self._service = service
        self._account_id = account_id
        self._args = (symbol, side, quantity, order_type, limit_price)
        self._order_id: str | None = None

    def execute(self) -> Order:
        symbol, side, quantity, order_type, limit_price = self._args
        order = self._service.place_order(self._account_id, symbol, side, quantity, order_type, limit_price)
        self._order_id = order.id
        return order

    def undo(self) -> Order:
        if self._order_id is None:
            raise ValidationError("cannot undo an order that was never placed")
        return self._service.cancel_order(self._account_id, self._order_id)


# --8<-- [end:commands]
