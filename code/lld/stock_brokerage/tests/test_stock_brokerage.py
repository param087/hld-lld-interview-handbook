from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, Money, SequentialIdGenerator
from lld.stock_brokerage.market import AlertService, MarketDataFeed, SimulatedExchange
from lld.stock_brokerage.models import (
    Account,
    AlertDirection,
    Fill,
    InsufficientFundsError,
    InsufficientHoldingsError,
    OrderSide,
    OrderStateError,
    OrderStatus,
    OrderType,
    PriceAlert,
    Quote,
    Stock,
    Watchlist,
)
from lld.stock_brokerage.orders import LimitOrder, MarketOrder
from lld.stock_brokerage.services import OrderService, PlaceOrderCommand, TradeLog
from lld.stock_brokerage.store import BrokerageStore


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000)


def build(clock: FakeClock, cash: str = "10000.00", max_fill: int | None = None) -> tuple[OrderService, MarketDataFeed, SimulatedExchange, TradeLog]:
    store = BrokerageStore()
    store.list_stock(Stock("AAPL", "Apple"))
    store.open_account(Account.open("acc-1", "ada", Money.of(cash)))
    feed = MarketDataFeed()
    exchange = SimulatedExchange(clock=clock, ids=SequentialIdGenerator("F"), max_fill_quantity=max_fill)
    log = TradeLog()
    service = OrderService(store, feed, exchange, clock=clock, ids=SequentialIdGenerator("O"), listeners=[log])
    exchange.connect(service.on_fill)
    feed.subscribe_all(exchange)
    return service, feed, exchange, log


def quote(feed: MarketDataFeed, clock: FakeClock, price: str, symbol: str = "AAPL") -> Quote:
    clock.advance(1)
    tick = Quote(symbol, Money.of(price), clock.now())
    feed.publish(tick)
    return tick


def test_market_buy_reserves_then_settles_at_the_traded_price(clock: FakeClock) -> None:
    service, feed, _, log = build(clock)
    quote(feed, clock, "100.00")
    order = service.place_order("acc-1", "AAPL", OrderSide.BUY, 10, OrderType.MARKET)
    assert order.status is OrderStatus.SUBMITTED
    assert service.account("acc-1").reserved_cash == Money.of("1050.00")  # 5% headroom on 100.00
    quote(feed, clock, "101.00")
    account = service.account("acc-1")
    assert account.cash == Money.of("8990.00") and account.reserved_cash == Money(0)
    assert account.portfolio.holding("AAPL").quantity == 10
    assert service.order("acc-1", order.id).status is OrderStatus.FILLED
    assert [t.notional for t in log.all()] == [Money.of("1010.00")]


def test_orders_beyond_the_available_balance_or_holding_never_reach_the_venue(clock: FakeClock) -> None:
    service, feed, exchange, _ = build(clock, cash="1000.00")
    quote(feed, clock, "100.00")
    with pytest.raises(InsufficientFundsError):
        service.place_order("acc-1", "AAPL", OrderSide.BUY, 50, OrderType.LIMIT, Money.of("100.00"))
    with pytest.raises(InsufficientHoldingsError):
        service.place_order("acc-1", "AAPL", OrderSide.SELL, 1, OrderType.LIMIT, Money.of("100.00"))
    account = service.account("acc-1")
    assert account.cash == Money.of("1000.00") and account.reserved_cash == Money(0)
    assert list(exchange.resting()) == [] and service.open_orders("acc-1") == []


def test_limit_order_rests_until_the_price_crosses_and_fills_at_the_better_price(clock: FakeClock) -> None:
    service, feed, _, _ = build(clock)
    quote(feed, clock, "100.00")
    order = service.place_order("acc-1", "AAPL", OrderSide.BUY, 10, OrderType.LIMIT, Money.of("95.00"))
    quote(feed, clock, "97.00")
    assert service.order("acc-1", order.id).status is OrderStatus.SUBMITTED
    quote(feed, clock, "94.00")
    filled = service.order("acc-1", order.id)
    assert filled.status is OrderStatus.FILLED and filled.average_price() == Money.of("94.00")
    assert service.account("acc-1").cash == Money.of("9060.00")


# --8<-- [start:partial]
def test_partial_fills_accumulate_and_cancelling_releases_the_remainder(clock: FakeClock) -> None:
    service, feed, _, _ = build(clock, max_fill=4)
    quote(feed, clock, "100.00")
    order = service.place_order("acc-1", "AAPL", OrderSide.BUY, 10, OrderType.LIMIT, Money.of("100.00"))
    assert service.account("acc-1").reserved_cash == Money.of("1000.00")  # 10 x the limit

    quote(feed, clock, "99.00")  # one slice of 4 shares
    resting = service.order("acc-1", order.id)
    assert resting.status is OrderStatus.PARTIALLY_FILLED and resting.filled_quantity == 4
    assert service.account("acc-1").reserved_cash == Money.of("600.00")  # 6 shares still held

    cancelled = service.cancel_order("acc-1", order.id)
    assert cancelled.status is OrderStatus.CANCELLED
    account = service.account("acc-1")
    assert account.reserved_cash == Money(0)
    assert account.cash == Money.of("9604.00")  # 10000 - 4 x 99.00
    assert account.portfolio.holding("AAPL").quantity == 4


# --8<-- [end:partial]


# --8<-- [start:idempotent]
def test_a_duplicate_execution_report_settles_exactly_once(clock: FakeClock) -> None:
    service, feed, exchange, _ = build(clock)
    seen: list[Fill] = []

    def record_and_settle(fill: Fill) -> None:
        seen.append(fill)
        service.on_fill(fill)

    exchange.connect(record_and_settle)
    quote(feed, clock, "100.00")
    service.place_order("acc-1", "AAPL", OrderSide.BUY, 10, OrderType.MARKET)
    quote(feed, clock, "100.00")

    first = service.trades("acc-1")[0]
    cash_after_first = service.account("acc-1").cash
    replayed = service.on_fill(seen[-1])  # the venue redelivers the same report

    assert replayed is first  # the stored trade comes back, nothing new is booked
    assert service.account("acc-1").cash == cash_after_first
    assert len(service.trades("acc-1")) == 1
    assert service.account("acc-1").portfolio.holding("AAPL").quantity == 10


# --8<-- [end:idempotent]


# --8<-- [start:concurrency]
def test_concurrent_orders_never_reserve_more_cash_than_the_account_holds(clock: FakeClock) -> None:
    service, feed, _, _ = build(clock, cash="5000.00")
    quote(feed, clock, "100.00")  # limit far below market, so nothing fills

    def place(i: int) -> bool:
        try:
            service.place_order("acc-1", "AAPL", OrderSide.BUY, 10, OrderType.LIMIT, Money.of("100.00"))
        except InsufficientFundsError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=10) as pool:
        accepted = list(pool.map(place, range(20)))

    account = service.account("acc-1")
    assert accepted.count(True) == 5  # 5 x 10 shares x 100.00 = the whole balance
    assert account.reserved_cash == Money.of("5000.00")
    assert account.available_cash() == Money(0)
    assert len(service.open_orders("acc-1")) == 5


# --8<-- [end:concurrency]


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (OrderStatus.FILLED, OrderStatus.CANCELLED),
        (OrderStatus.CANCELLED, OrderStatus.FILLED),
        (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.REJECTED, OrderStatus.SUBMITTED),
    ],
)
def test_the_order_state_machine_refuses_illegal_transitions(start: OrderStatus, target: OrderStatus) -> None:
    order = MarketOrder(
        id="O-1", account_id="acc-1", symbol="AAPL", side=OrderSide.BUY, quantity=1, created_at=0.0, status=start
    )
    with pytest.raises(OrderStateError):
        order.transition_to(target)


def test_a_fill_arriving_after_a_cancel_is_refused(clock: FakeClock) -> None:
    service, feed, exchange, _ = build(clock)
    seen: list[Fill] = []
    exchange.connect(seen.append)  # capture the report instead of settling it
    quote(feed, clock, "100.00")
    order = service.place_order("acc-1", "AAPL", OrderSide.BUY, 10, OrderType.LIMIT, Money.of("100.00"))
    quote(feed, clock, "99.00")
    service.cancel_order("acc-1", order.id)
    with pytest.raises(OrderStateError):
        service.on_fill(seen[-1])
    assert service.account("acc-1").reserved_cash == Money(0)


def test_alerts_fire_once_and_the_watchlist_follows_the_feed(clock: FakeClock) -> None:
    feed = MarketDataFeed()
    alerts = AlertService(clock=clock)
    alerts.register(PriceAlert("al-1", "ada", "AAPL", AlertDirection.ABOVE, Money.of("110.00")))
    watchlist = Watchlist("wl-1", "ada", {"AAPL"})
    feed.subscribe_all(alerts)
    feed.subscribe_all(watchlist)

    quote(feed, clock, "100.00")
    assert alerts.triggered() == [] and watchlist.render() == "AAPL 100.00 USD"
    quote(feed, clock, "111.00")
    assert [a.id for a in alerts.triggered()] == ["al-1"]
    quote(feed, clock, "112.00")
    assert len(alerts.triggered()) == 1  # fires once, not on every tick above the threshold


def test_place_order_command_can_be_undone(clock: FakeClock) -> None:
    service, feed, _, _ = build(clock)
    quote(feed, clock, "100.00")
    command = PlaceOrderCommand(service, "acc-1", "AAPL", OrderSide.BUY, 5, OrderType.LIMIT, Money.of("90.00"))
    order = command.execute()
    assert isinstance(order, LimitOrder) and service.account("acc-1").reserved_cash == Money.of("450.00")
    assert command.undo().status is OrderStatus.CANCELLED
    assert service.account("acc-1").reserved_cash == Money(0)
