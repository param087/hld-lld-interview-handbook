"""One session: a market buy, a limit buy that fills in two slices, a duplicate report, a sell."""

from common import FakeClock, Money, SequentialIdGenerator
from lld.stock_brokerage.market import AlertService, MarketDataFeed, SimulatedExchange
from lld.stock_brokerage.models import (
    Account,
    AlertDirection,
    Fill,
    OrderSide,
    OrderType,
    PriceAlert,
    Quote,
    Stock,
    Watchlist,
)
from lld.stock_brokerage.services import OrderService
from lld.stock_brokerage.store import BrokerageStore


def tick(feed: MarketDataFeed, clock: FakeClock, symbol: str, price: str) -> None:
    clock.advance(60)
    feed.publish(Quote(symbol, Money.of(price), clock.now()))


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    store = BrokerageStore()
    store.list_stock(Stock("AAPL", "Apple"))
    store.list_stock(Stock("MSFT", "Microsoft"))
    store.open_account(Account.open("acc-1", "ada", Money.of("50000.00")))

    feed = MarketDataFeed()
    exchange = SimulatedExchange(clock=clock, ids=SequentialIdGenerator("F"), max_fill_quantity=20)
    service = OrderService(store, feed, exchange, clock=clock, ids=SequentialIdGenerator("O"))
    fills: list[Fill] = []

    def settle(fill: Fill) -> None:
        fills.append(fill)
        service.on_fill(fill)

    exchange.connect(settle)
    alerts = AlertService(clock=clock)
    alerts.register(PriceAlert("al-1", "ada", "AAPL", AlertDirection.ABOVE, Money.of("190.00")))
    watchlist = Watchlist("wl-1", "ada", {"AAPL", "MSFT"})
    for listener in (exchange, alerts, watchlist):
        feed.subscribe_all(listener)

    tick(feed, clock, "AAPL", "185.00")
    tick(feed, clock, "MSFT", "400.00")
    buy = service.place_order("acc-1", "AAPL", OrderSide.BUY, 20, OrderType.MARKET)
    print(f"{buy.id} market BUY 20 AAPL -> {buy.status}, reserved {service.account('acc-1').reserved_cash}")
    tick(feed, clock, "AAPL", "186.50")
    account = service.account("acc-1")
    print(f"filled at {service.order('acc-1', buy.id).average_price()}, cash {account.cash}, reserved {account.reserved_cash}")

    limit = service.place_order("acc-1", "MSFT", OrderSide.BUY, 30, OrderType.LIMIT, Money.of("395.00"))
    print(f"{limit.id} limit BUY 30 MSFT at 395.00 -> {limit.status}, reserved {service.account('acc-1').reserved_cash}")
    tick(feed, clock, "MSFT", "398.00")
    print(f"tick 398.00: still {service.order('acc-1', limit.id).status}, 0 shares")
    tick(feed, clock, "MSFT", "394.00")
    partial = service.order("acc-1", limit.id)
    print(f"tick 394.00: {partial.status} {partial.filled_quantity}/{partial.quantity}, cash {service.account('acc-1').cash}")
    service.on_fill(fills[-1])
    print(f"duplicate report {fills[-1].fill_id} replayed: cash still {service.account('acc-1').cash}")
    tick(feed, clock, "MSFT", "393.00")
    done = service.order("acc-1", limit.id)
    print(f"tick 393.00: {done.status} at average {done.average_price()}, cash {service.account('acc-1').cash}")

    sell = service.place_order("acc-1", "AAPL", OrderSide.SELL, 10, OrderType.LIMIT, Money.of("190.00"))
    tick(feed, clock, "AAPL", "191.00")
    print(f"{sell.id} limit SELL 10 AAPL at 190.00 -> {service.order('acc-1', sell.id).status} at 191.00")
    print(f"alerts fired: {[a.id for a in alerts.triggered()]}")

    stale = service.place_order("acc-1", "AAPL", OrderSide.BUY, 5, OrderType.LIMIT, Money.of("150.00"))
    print(f"{stale.id} rests off-market, reserving {service.account('acc-1').reserved_cash}")
    service.cancel_order("acc-1", stale.id)
    account = service.account("acc-1")
    print(f"cancelled: reserved back to {account.reserved_cash}, cash {account.cash}")
    prices = {"AAPL": Money.of("191.00"), "MSFT": Money.of("393.00")}
    print(f"portfolio {account.portfolio.value(prices)} across {len(service.trades('acc-1'))} trades")
    print(f"watchlist: {watchlist.render()}")


if __name__ == "__main__":
    main()
