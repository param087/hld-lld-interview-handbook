import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import NotFoundError, ValidationError
from hld.matching_engine import (
    CancelOrder,
    MatchingEngine,
    NewOrder,
    OrderStatus,
    RiskLimits,
    Sequencer,
    Side,
    TimeInForce,
)


class Exchange:
    """Sequencer plus engine, so tests exercise the same path as a gateway."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.sequencer = Sequencer()
        self.engine = MatchingEngine("ACME", limits)

    def send(self, command: NewOrder | CancelOrder) -> list:
        return self.engine.apply(self.sequencer.submit(command), command)


@pytest.fixture
def exchange() -> Exchange:
    return Exchange()


def test_a_limit_order_that_does_not_cross_rests_on_the_book(exchange: Exchange) -> None:
    exchange.send(NewOrder("s1", "mm", Side.SELL, 100, 1010))
    exchange.send(NewOrder("b1", "fund", Side.BUY, 50, 1000))
    assert exchange.engine.trades == []
    assert exchange.engine.book.best(Side.BUY) == 1000
    assert exchange.engine.book.best(Side.SELL) == 1010
    assert exchange.engine.order("b1").status is OrderStatus.NEW


def test_price_time_priority_fills_the_oldest_order_at_the_best_price(exchange: Exchange) -> None:
    exchange.send(NewOrder("s_old", "mm", Side.SELL, 30, 1010))
    exchange.send(NewOrder("s_new", "mm", Side.SELL, 30, 1010))
    exchange.send(NewOrder("s_best", "mm", Side.SELL, 10, 1005))  # better price jumps the queue
    trades = exchange.send(NewOrder("b1", "hedge", Side.BUY, 45, 1010))
    assert [(t.sell_order_id, t.quantity, t.price) for t in trades] == [
        ("s_best", 10, 1005),
        ("s_old", 30, 1010),
        ("s_new", 5, 1010),
    ]
    assert exchange.engine.order("s_new").remaining == 25


def test_partial_fill_leaves_the_remainder_resting_and_reduces_depth(exchange: Exchange) -> None:
    exchange.send(NewOrder("s1", "mm", Side.SELL, 100, 1010))
    exchange.send(NewOrder("b1", "hedge", Side.BUY, 40, 1010))
    maker = exchange.engine.order("s1")
    taker = exchange.engine.order("b1")
    assert (maker.status, maker.remaining, maker.filled_quantity) == (
        OrderStatus.PARTIALLY_FILLED,
        60,
        40,
    )
    assert (taker.status, taker.remaining) == (OrderStatus.FILLED, 0)
    assert exchange.engine.book.depth(Side.SELL) == [(1010, 60)]


def test_the_trade_prints_at_the_resting_price_so_the_taker_improves(exchange: Exchange) -> None:
    exchange.send(NewOrder("s1", "mm", Side.SELL, 10, 1005))
    (trade,) = exchange.send(NewOrder("b1", "hedge", Side.BUY, 10, 1050))
    assert trade.price == 1005
    assert trade.aggressor is Side.BUY
    assert exchange.engine.last_price == 1005


@pytest.mark.parametrize(
    ("price", "tif"),
    [(None, TimeInForce.GTC), (900, TimeInForce.IOC)],
)
def test_market_and_ioc_orders_cancel_their_remainder(
    exchange: Exchange, price: int | None, tif: TimeInForce
) -> None:
    exchange.send(NewOrder("b1", "fund", Side.BUY, 40, 1000))
    exchange.send(NewOrder("s1", "retail", Side.SELL, 60, price, tif))
    order = exchange.engine.order("s1")
    assert (order.status, order.filled_quantity, order.remaining) == (OrderStatus.CANCELLED, 40, 20)
    assert exchange.engine.book.best(Side.SELL) is None  # nothing rested


def test_cancel_is_lazy_and_the_matcher_walks_past_dead_orders(exchange: Exchange) -> None:
    exchange.send(NewOrder("s_first", "mm", Side.SELL, 30, 1010))
    exchange.send(NewOrder("s_second", "mm", Side.SELL, 30, 1010))
    exchange.send(CancelOrder("s_first"))
    assert exchange.engine.book.depth(Side.SELL) == [(1010, 30)]
    trades = exchange.send(NewOrder("b1", "hedge", Side.BUY, 30, 1010))
    assert [t.sell_order_id for t in trades] == ["s_second"]
    with pytest.raises(NotFoundError):
        exchange.send(CancelOrder("s_first"))  # already gone from the resting index


def test_emptied_price_levels_disappear_from_the_book(exchange: Exchange) -> None:
    exchange.send(NewOrder("s1", "mm", Side.SELL, 10, 1005))
    exchange.send(NewOrder("b1", "hedge", Side.BUY, 10, 1005))
    assert exchange.engine.book.best(Side.SELL) is None
    exchange.send(NewOrder("s2", "mm", Side.SELL, 10, 1005))  # the same price comes back
    assert exchange.engine.book.best(Side.SELL) == 1005
    assert exchange.engine.book.depth(Side.SELL) == [(1005, 10)]


def test_pre_trade_risk_rejects_without_touching_the_book() -> None:
    exchange = Exchange(RiskLimits(max_order_quantity=100, price_band_bps=500))
    exchange.send(NewOrder("s1", "mm", Side.SELL, 10, 1000))
    exchange.send(NewOrder("b1", "fund", Side.BUY, 10, 1000))  # last_price becomes 1000
    exchange.send(NewOrder("big", "fund", Side.BUY, 500, 1000))
    exchange.send(NewOrder("far", "fund", Side.BUY, 10, 2000))  # outside the +-5% collar
    rejected = exchange.engine.order("big")
    assert rejected.status is OrderStatus.REJECTED
    assert "above the 100 cap" in (rejected.reject_reason or "")
    assert (rejected.remaining, rejected.filled_quantity) == (500, 0)  # nothing was worked
    assert exchange.engine.order("far").status is OrderStatus.REJECTED
    assert "collar" in (exchange.engine.order("far").reject_reason or "")
    assert exchange.engine.book.best(Side.BUY) is None


def test_the_open_order_count_falls_again_when_a_resting_order_fills() -> None:
    """The cap counts *open* orders, so filled and cancelled ones must give their slot back."""
    exchange = Exchange(RiskLimits(max_open_orders_per_account=2))
    exchange.send(NewOrder("m1", "mm", Side.SELL, 10, 1010))
    exchange.send(NewOrder("m2", "mm", Side.SELL, 10, 1011))
    assert exchange.send(NewOrder("m3", "mm", Side.SELL, 10, 1012)) == []
    assert exchange.engine.order("m3").status is OrderStatus.REJECTED  # the cap bites

    exchange.send(NewOrder("t1", "hedge", Side.BUY, 20, 1011))  # sweeps m1 and m2 completely
    assert exchange.engine.order("m1").status is OrderStatus.FILLED
    exchange.send(NewOrder("m4", "mm", Side.SELL, 10, 1012))
    exchange.send(NewOrder("m5", "mm", Side.SELL, 10, 1013))
    assert exchange.engine.order("m5").status is OrderStatus.NEW  # both slots came back


def test_a_partially_filled_resting_order_still_counts_against_the_open_cap() -> None:
    """The other edge of the same rule: only a *fully* filled maker gives its slot back."""
    exchange = Exchange(RiskLimits(max_open_orders_per_account=1))
    exchange.send(NewOrder("m1", "mm", Side.SELL, 100, 1010))
    exchange.send(NewOrder("t1", "hedge", Side.BUY, 40, 1010))  # partial: m1 keeps resting
    assert exchange.engine.order("m1").status is OrderStatus.PARTIALLY_FILLED
    exchange.send(NewOrder("m2", "mm", Side.SELL, 10, 1011))
    assert exchange.engine.order("m2").status is OrderStatus.REJECTED
    exchange.send(CancelOrder("m1"))
    exchange.send(NewOrder("m3", "mm", Side.SELL, 10, 1011))
    assert exchange.engine.order("m3").status is OrderStatus.NEW


def test_replaying_the_journal_rebuilds_an_identical_engine() -> None:
    rng = random.Random(42)
    exchange = Exchange()
    live: list[str] = []
    resting = (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)
    for i in range(300):
        live = [oid for oid in live if exchange.engine.order(oid).status in resting]
        if live and rng.random() < 0.2:
            exchange.send(CancelOrder(live.pop(rng.randrange(len(live)))))
            continue
        order_id = f"o{i}"
        exchange.send(
            NewOrder(
                order_id,
                f"acct{i % 5}",
                rng.choice([Side.BUY, Side.SELL]),
                rng.randint(1, 50),
                rng.randint(990, 1020),
            )
        )
        if exchange.engine.order(order_id).status in resting:
            live.append(order_id)

    standby = MatchingEngine.replay("ACME", exchange.sequencer.journal())
    assert len(exchange.engine.trades) > 20  # the scenario really traded
    assert standby.trades == exchange.engine.trades
    assert standby.last_price == exchange.engine.last_price
    for side in (Side.BUY, Side.SELL):
        assert standby.book.depth(side, 10) == exchange.engine.book.depth(side, 10)


def test_concurrent_gateways_get_a_gap_free_total_order() -> None:
    sequencer = Sequencer()
    commands = [NewOrder(f"o{i}", "acct", Side.BUY, 1, 1000) for i in range(500)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(sequencer.submit, commands))
    assert sorted(sequences) == list(range(1, 501))
    journal = sequencer.journal()
    assert [seq for seq, _ in journal] == list(range(1, 501))
    # Whatever interleaving happened, the engine's view is one deterministic order.
    engine = MatchingEngine.replay("ACME", journal, RiskLimits(max_open_orders_per_account=1_000))
    assert engine.book.depth(Side.BUY) == [(1000, 500)]


def test_gateway_validation_happens_before_sequencing() -> None:
    with pytest.raises(ValidationError):
        NewOrder("bad", "acct", Side.BUY, 0, 1000)
    with pytest.raises(ValidationError):
        NewOrder("bad", "acct", Side.BUY, 10, -5)
    with pytest.raises(NotFoundError):
        MatchingEngine("ACME").order("never-sent")
