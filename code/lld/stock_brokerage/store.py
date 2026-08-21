"""The account store and the Unit of Work that settles a fill atomically.

Locking rule: ``account_lock(account_id)`` is the one lock that matters -- cash,
reservations, holdings and orders of a single account move together under it.
The registry lock inside the store is held only for dictionary lookups.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol, Self

from lld.stock_brokerage.models import Account, Stock, Trade, UnknownEntityError
from lld.stock_brokerage.orders import Order


# --8<-- [start:store]
@dataclass(slots=True)
class AccountState:
    """Everything one account owns. The unit the Unit of Work copies and republishes."""

    account: Account
    orders: dict[str, Order] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    trades_by_fill: dict[str, Trade] = field(default_factory=dict)

    def copy(self) -> AccountState:
        return AccountState(
            self.account.copy(),
            {order_id: order.copy() for order_id, order in self.orders.items()},
            list(self.trades),
            dict(self.trades_by_fill),
        )


class BrokerageStore:
    """Accounts, the symbol master and the order-to-account index."""

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._accounts: dict[str, AccountState] = {}
        self._account_locks: dict[str, threading.Lock] = {}
        self._stocks: dict[str, Stock] = {}
        self._order_index: dict[str, str] = {}  # order id -> account id

    def list_stock(self, stock: Stock) -> Stock:
        with self._registry_lock:
            self._stocks[stock.symbol] = stock
        return stock

    def stock(self, symbol: str) -> Stock:
        with self._registry_lock:
            try:
                return self._stocks[symbol]
            except KeyError:
                raise UnknownEntityError(f"unknown symbol {symbol}") from None

    def open_account(self, account: Account) -> Account:
        with self._registry_lock:
            self._accounts[account.id] = AccountState(account)
            self._account_locks[account.id] = threading.Lock()
        return account

    def account_lock(self, account_id: str) -> threading.Lock:
        with self._registry_lock:
            try:
                return self._account_locks[account_id]
            except KeyError:
                raise UnknownEntityError(f"unknown account {account_id}") from None

    def index_order(self, order_id: str, account_id: str) -> None:
        with self._registry_lock:
            self._order_index[order_id] = account_id

    def account_for_order(self, order_id: str) -> str:
        with self._registry_lock:
            try:
                return self._order_index[order_id]
            except KeyError:
                raise UnknownEntityError(f"unknown order {order_id}") from None

    def snapshot(self, account_id: str) -> AccountState:
        with self._registry_lock:
            try:
                return self._accounts[account_id].copy()
            except KeyError:
                raise UnknownEntityError(f"unknown account {account_id}") from None

    def publish(self, state: AccountState) -> None:
        with self._registry_lock:
            self._accounts[state.account.id] = state


# --8<-- [end:store]


# --8<-- [start:uow]
class UnitOfWork(Protocol):
    """One transaction boundary over one account. ``state`` is the working copy."""

    state: AccountState

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class AccountUnitOfWork:
    """Cash, reservation, holding, order status and the trade row commit together.

    A settled fill touches five things. If the process died between the debit
    and the holding update, the account would have paid for shares it does not
    own -- so all five happen on a private copy that ``commit`` publishes in one
    assignment, and any exception leaves the stored state untouched.
    """

    def __init__(self, store: BrokerageStore, account_id: str) -> None:
        self._store = store
        self._account_id = account_id
        self._committed = False
        self.state = store.snapshot(account_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._committed:
            self.rollback()

    def commit(self) -> None:
        self._store.publish(self.state)
        self._committed = True

    def rollback(self) -> None:
        self.state = self._store.snapshot(self._account_id)


# --8<-- [end:uow]
