"""A price-time-priority limit order book with partial fills, a sequencer and replay.

The crux of the stock-exchange design in one module:

* :class:`OrderBook` keeps one heap of price levels per side and a FIFO queue inside each
  level, so the best price is O(1) to peek and time priority inside a price is free.
* Matching walks price levels best-first and fills FIFO, decrementing ``remaining`` on both
  sides; the trade prints at the **resting** order's price, so price improvement goes to the
  aggressor.
* :class:`Sequencer` is the only concurrent component: gateway threads take a gap-free
  sequence number under one lock and the command lands in a journal.
* :class:`MatchingEngine` is single-threaded per symbol and holds no lock at all. Because its
  only input is the journal and it never reads a clock, ``replay()`` rebuilds a byte-identical
  book -- which is how a hot standby takes over without losing a trade.

Prices are integer ticks (cents). Time priority is *sequence* priority: wall clocks are never
consulted, because they are not monotonic across machines.
"""

from __future__ import annotations

import heapq
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from common import InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:models]
class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class TimeInForce(StrEnum):
    GTC = "gtc"  # rest on the book until filled or cancelled
    IOC = "ioc"  # match what you can right now, cancel the rest


class OrderStatus(StrEnum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class NewOrder:
    """A gateway-validated order request, before it is sequenced."""

    order_id: str
    account_id: str
    side: Side
    quantity: int
    price: int | None = None  # None = market order, which never rests
    tif: TimeInForce = TimeInForce.GTC

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValidationError("quantity must be positive")
        if self.price is not None and self.price <= 0:
            raise ValidationError("limit price must be a positive number of ticks")


@dataclass(frozen=True, slots=True)
class CancelOrder:
    order_id: str


Command = NewOrder | CancelOrder


@dataclass(slots=True)
class Order:
    """A resting or in-flight order. ``sequence`` is its position in time priority."""

    order_id: str
    account_id: str
    side: Side
    quantity: int
    price: int | None
    tif: TimeInForce
    sequence: int
    remaining: int
    status: OrderStatus = OrderStatus.NEW
    reject_reason: str | None = None

    @property
    def filled_quantity(self) -> int:
        return self.quantity - self.remaining


@dataclass(frozen=True, slots=True)
class Trade:
    """An execution. The id is derived from the sequence, so replay reproduces it exactly."""

    trade_id: str
    symbol: str
    price: int
    quantity: int
    buy_order_id: str
    sell_order_id: str
    aggressor: Side
    sequence: int


# --8<-- [end:models]


# --8<-- [start:book]
def is_live(order: Order) -> bool:
    """Still executable: not cancelled and not fully filled. Cancels are lazy, so a dead
    order stays in its FIFO queue until the matching loop walks past it."""
    return order.remaining > 0 and order.status is not OrderStatus.CANCELLED


class OrderBook:
    """Price-time priority: a heap of price levels per side, a FIFO deque inside each level.

    Bid prices are negated so both sides use Python's min-heap. A level is removed from
    ``_levels`` only when its heap entry is popped, so a price is never pushed twice and no
    stale entry can shadow a live level.
    """

    def __init__(self) -> None:
        self._bids: list[int] = []  # negated prices: -heap[0] is the best bid
        self._asks: list[int] = []  # heap[0] is the best ask
        self._levels: dict[tuple[Side, int], deque[Order]] = {}
        self._resting: dict[str, Order] = {}

    def best(self, side: Side) -> int | None:
        """Best price on one side, discarding levels emptied by fills and cancels."""
        heap = self._bids if side is Side.BUY else self._asks
        while heap:
            price = -heap[0] if side is Side.BUY else heap[0]
            level = self._levels.get((side, price))
            if level and any(is_live(o) for o in level):
                return price
            heapq.heappop(heap)
            self._levels.pop((side, price), None)
        return None

    def depth(self, side: Side, levels: int = 5) -> list[tuple[int, int]]:
        """Aggregated (price, quantity) top of book -- exactly what market data publishes."""
        rows = [
            (price, sum(o.remaining for o in queue if is_live(o)))
            for (order_side, price), queue in self._levels.items()
            if order_side is side and any(is_live(o) for o in queue)
        ]
        rows.sort(key=lambda row: row[0], reverse=side is Side.BUY)
        return rows[:levels]

    def rest(self, order: Order) -> None:
        if order.price is None:
            raise InvalidStateError("a market order never rests on the book")
        key = (order.side, order.price)
        queue = self._levels.get(key)
        if queue is None:
            self._levels[key] = deque([order])
            heap = self._bids if order.side is Side.BUY else self._asks
            heapq.heappush(heap, -order.price if order.side is Side.BUY else order.price)
        else:
            queue.append(order)
        self._resting[order.order_id] = order

    def cancel(self, order_id: str) -> Order:
        """O(1) cancel: flag the order and let the matching loop skip it (lazy deletion)."""
        order = self._resting.pop(order_id, None)
        if order is None:
            raise NotFoundError(f"no resting order {order_id!r}")
        order.status = OrderStatus.CANCELLED  # remaining is kept, so fills stay auditable
        return order

    def crosses(self, taker: Order, best_price: int) -> bool:
        if taker.price is None:  # market order takes whatever is there
            return True
        return taker.price >= best_price if taker.side is Side.BUY else taker.price <= best_price

    def match(self, taker: Order, symbol: str) -> list[Trade]:
        """Fill ``taker`` against the book, best price first and FIFO inside a price."""
        trades: list[Trade] = []
        while taker.remaining > 0:
            best_price = self.best(taker.side.opposite)
            if best_price is None or not self.crosses(taker, best_price):
                break
            queue = self._levels[(taker.side.opposite, best_price)]
            while queue and taker.remaining > 0:
                maker = queue[0]
                if not is_live(maker):  # cancelled, or filled by an earlier sweep
                    queue.popleft()
                    continue
                quantity = min(taker.remaining, maker.remaining)
                taker.remaining -= quantity
                maker.remaining -= quantity
                buy_id, sell_id = (
                    (taker.order_id, maker.order_id)
                    if taker.side is Side.BUY
                    else (maker.order_id, taker.order_id)
                )
                trades.append(
                    Trade(
                        trade_id=f"{symbol}-{taker.sequence}-{len(trades) + 1}",
                        symbol=symbol,
                        price=best_price,  # the maker's price: improvement goes to the taker
                        quantity=quantity,
                        buy_order_id=buy_id,
                        sell_order_id=sell_id,
                        aggressor=taker.side,
                        sequence=taker.sequence,
                    )
                )
                if maker.remaining == 0:
                    maker.status = OrderStatus.FILLED
                    queue.popleft()
                    self._resting.pop(maker.order_id, None)
                else:
                    maker.status = OrderStatus.PARTIALLY_FILLED
        return trades


# --8<-- [end:book]


# --8<-- [start:engine]
@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Pre-trade checks. They run inside the engine so they cannot be bypassed by a gateway."""

    max_order_quantity: int = 10_000
    price_band_bps: int = 1_000  # a limit price must sit within +-10% of the last trade
    max_open_orders_per_account: int = 100


class Sequencer:
    """The single point of ordering. Gateways call it concurrently; the journal is the truth.

    ``_lock`` guards ``_journal``; nothing else in this module is shared between threads.
    """

    def __init__(self) -> None:
        self._journal: list[tuple[int, Command]] = []
        self._lock = threading.Lock()

    def submit(self, command: Command) -> int:
        with self._lock:
            sequence = len(self._journal) + 1
            self._journal.append((sequence, command))
            return sequence

    def journal(self) -> list[tuple[int, Command]]:
        with self._lock:
            return list(self._journal)


class MatchingEngine:
    """One symbol, one book, one thread. No locks: ordering already happened in the sequencer."""

    def __init__(self, symbol: str, limits: RiskLimits | None = None) -> None:
        self.symbol = symbol
        self.limits = limits or RiskLimits()
        self.book = OrderBook()
        self.trades: list[Trade] = []
        self.last_price: int | None = None
        self._orders: dict[str, Order] = {}
        self._open_per_account: dict[str, int] = {}

    def apply(self, sequence: int, command: Command) -> list[Trade]:
        """Process one sequenced command. Deterministic: same journal, same output."""
        if isinstance(command, CancelOrder):
            order = self.book.cancel(command.order_id)
            self._open_per_account[order.account_id] -= 1
            return []
        order = Order(
            order_id=command.order_id,
            account_id=command.account_id,
            side=command.side,
            quantity=command.quantity,
            price=command.price,
            tif=command.tif,
            sequence=sequence,
            remaining=command.quantity,
        )
        self._orders[order.order_id] = order
        if reason := self._risk_reason(order):
            # Recorded, never raised: ``remaining`` stays untouched so ``filled_quantity`` is 0.
            order.status, order.reject_reason = OrderStatus.REJECTED, reason
            return []
        trades = self.book.match(order, self.symbol)
        self.trades.extend(trades)
        self._release_filled_makers(trades)
        if trades:
            self.last_price = trades[-1].price
        if order.remaining == 0:
            order.status = OrderStatus.FILLED
        elif order.price is None or order.tif is TimeInForce.IOC:
            order.status = OrderStatus.CANCELLED  # the remainder never rests
        else:
            order.status = OrderStatus.PARTIALLY_FILLED if trades else OrderStatus.NEW
            self.book.rest(order)
            self._open_per_account[order.account_id] = (
                self._open_per_account.get(order.account_id, 0) + 1
            )
        return trades

    def _release_filled_makers(self, trades: list[Trade]) -> None:
        """A resting order that just filled completely is no longer open.

        ``_open_per_account`` is incremented when an order rests and decremented on cancel, so
        without this the counter only ever grows and an account with nothing on the book is
        eventually rejected for having "too many open orders". A maker appears at most once per
        sweep, because the matcher pops it from its queue the moment ``remaining`` hits zero.
        """
        for trade in trades:
            maker_id = trade.sell_order_id if trade.aggressor is Side.BUY else trade.buy_order_id
            maker = self._orders[maker_id]
            if maker.status is OrderStatus.FILLED:
                self._open_per_account[maker.account_id] -= 1

    def _risk_reason(self, order: Order) -> str | None:
        if order.quantity > self.limits.max_order_quantity:
            return f"quantity {order.quantity} above the {self.limits.max_order_quantity} cap"
        if self._open_per_account.get(order.account_id, 0) >= self.limits.max_open_orders_per_account:
            return "too many open orders for this account"
        if order.price is not None and self.last_price is not None:
            band = self.last_price * self.limits.price_band_bps // 10_000
            if not self.last_price - band <= order.price <= self.last_price + band:
                return f"price {order.price} outside the collar around {self.last_price}"
        return None

    def order(self, order_id: str) -> Order:
        if order_id not in self._orders:
            raise NotFoundError(f"unknown order {order_id!r}")
        return self._orders[order_id]

    @classmethod
    def replay(
        cls, symbol: str, journal: list[tuple[int, Command]], limits: RiskLimits | None = None
    ) -> MatchingEngine:
        """Rebuild an identical engine from the journal -- the whole fault-tolerance story."""
        engine = cls(symbol, limits)
        for sequence, command in journal:
            engine.apply(sequence, command)
        return engine


# --8<-- [end:engine]


@dataclass(slots=True)
class _DemoGateway:
    """Sequencer plus engine, the way a gateway thread sees the exchange."""

    sequencer: Sequencer
    engine: MatchingEngine
    printed: list[str] = field(default_factory=list)

    def send(self, command: Command) -> list[Trade]:
        return self.engine.apply(self.sequencer.submit(command), command)


def _ticks(price: int) -> str:
    return str(Money(price)).removesuffix(" USD")


def main() -> None:
    gateway = _DemoGateway(Sequencer(), MatchingEngine("ACME"))
    resting = [
        NewOrder("s1", "mm", Side.SELL, 50, 1005),
        NewOrder("s2", "mm", Side.SELL, 100, 1010),
        NewOrder("b1", "fund", Side.BUY, 40, 1000),
    ]
    for command in resting:
        gateway.send(command)
    print(f"book: bids={[(_ticks(p), q) for p, q in gateway.engine.book.depth(Side.BUY)]}")
    print(f"      asks={[(_ticks(p), q) for p, q in gateway.engine.book.depth(Side.SELL)]}")

    print("aggressive buy 120 @ 10.10 sweeps two price levels:")
    for trade in gateway.send(NewOrder("b2", "hedge", Side.BUY, 120, 1010)):
        print(f"  {trade.trade_id}  {trade.quantity} @ {_ticks(trade.price)}")
    partial = gateway.engine.order("s2")
    print(f"  s2 is {partial.status} with {partial.remaining} left of {partial.quantity}")

    print("market sell 60 walks down the bids and cancels what it cannot fill:")
    for trade in gateway.send(NewOrder("s3", "retail", Side.SELL, 60, None)):
        print(f"  {trade.trade_id}  {trade.quantity} @ {_ticks(trade.price)}")
    market = gateway.engine.order("s3")
    print(f"  s3 is {market.status} after filling {market.filled_quantity} of {market.quantity}")

    standby = MatchingEngine.replay("ACME", gateway.sequencer.journal())
    print(f"replayed {len(gateway.sequencer.journal())} commands into a standby engine")
    print(f"  trades identical: {standby.trades == gateway.engine.trades}")
    print(f"  book identical:   {standby.book.depth(Side.SELL) == gateway.engine.book.depth(Side.SELL)}")


if __name__ == "__main__":
    main()
