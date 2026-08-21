"""Observer: the subject fans each event out to whoever subscribed and never learns what they do."""

from __future__ import annotations

import gc
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import Money
from patterns.observer import PriceAlert, PriceFeed, PriceTick, Signal, Watchlist


class Recorder:
    """A test observer: remembers every tick it was given. Needs no base class."""

    def __init__(self) -> None:
        self.ticks: list[PriceTick] = []
        self._lock = threading.Lock()

    def on_price(self, tick: PriceTick) -> None:
        with self._lock:
            self.ticks.append(tick)


class Broken:
    def on_price(self, tick: PriceTick) -> None:
        raise RuntimeError("downstream store unavailable")


def test_every_subscriber_gets_every_tick_and_filters_for_itself() -> None:
    feed = PriceFeed()
    tech = Watchlist("tech", ["AAPL", "MSFT"])
    alert = PriceAlert("AAPL", above=Money.of("200.00"))
    recorder = Recorder()
    for observer in (tech, alert, recorder):
        feed.subscribe(observer)

    assert feed.publish("AAPL", Money.of("199.50")) == 3
    assert feed.publish("XOM", Money.of("105.10")) == 3
    assert feed.publish("AAPL", Money.of("201.25")) == 3

    assert tech.latest() == {"AAPL": Money.of("201.25")}  # XOM was filtered out by the watchlist
    assert alert.fired_at == PriceTick("AAPL", Money.of("201.25"), 3)
    assert [tick.seq for tick in recorder.ticks] == [1, 2, 3]


def test_subscribe_is_idempotent_and_unsubscribe_stops_delivery() -> None:
    feed = PriceFeed()
    recorder = Recorder()
    feed.subscribe(recorder)
    feed.subscribe(recorder)
    assert feed.observer_count == 1
    feed.publish("AAPL", Money.of("1.00"))
    assert len(recorder.ticks) == 1  # once, not twice

    feed.unsubscribe(recorder)
    feed.unsubscribe(recorder)  # a second unsubscribe is harmless
    assert feed.observer_count == 0
    feed.publish("AAPL", Money.of("2.00"))
    assert len(recorder.ticks) == 1


def test_a_raising_observer_is_isolated_and_reported_to_the_error_policy() -> None:
    failures: list[tuple[object, int, str]] = []
    feed = PriceFeed(on_error=lambda obs, tick, exc: failures.append((obs, tick.seq, str(exc))))
    before, broken, after = Recorder(), Broken(), Recorder()
    for observer in (before, broken, after):
        feed.subscribe(observer)

    delivered = feed.publish("AAPL", Money.of("1.00"))

    assert delivered == 2
    assert len(before.ticks) == len(after.ticks) == 1  # the observer after the broken one still ran
    assert failures == [(broken, 1, "downstream store unavailable")]
    assert feed.observer_count == 3  # isolation is not eviction: the policy decides what happens next


def test_an_observer_may_unsubscribe_itself_while_being_notified() -> None:
    feed = PriceFeed()

    class OneShot:
        def __init__(self) -> None:
            self.ticks = 0

        def on_price(self, tick: PriceTick) -> None:
            self.ticks += 1
            feed.unsubscribe(self)  # mutates the list during dispatch: safe, the feed iterates a copy

    one_shot, recorder = OneShot(), Recorder()
    feed.subscribe(one_shot)
    feed.subscribe(recorder)
    feed.publish("AAPL", Money.of("1.00"))
    feed.publish("AAPL", Money.of("2.00"))
    assert one_shot.ticks == 1
    assert len(recorder.ticks) == 2
    assert feed.observer_count == 1


@pytest.mark.parametrize(
    ("prices", "fired_seq"),
    [
        (["199.00", "200.00"], None),  # at the threshold is not above it
        (["200.01"], 1),
        (["150.00", "250.00", "300.00"], 2),  # fires once, on the first breach
    ],
)
def test_alert_fires_once_on_the_first_breach(prices: list[str], fired_seq: int | None) -> None:
    feed = PriceFeed()
    alert = PriceAlert("AAPL", above=Money.of("200.00"))
    feed.subscribe(alert)
    for price in prices:
        feed.publish("AAPL", Money.of(price))
    assert (alert.fired_at.seq if alert.fired_at else None) == fired_seq


def test_concurrent_publishers_and_subscribers_neither_lose_nor_duplicate_ticks() -> None:
    feed = PriceFeed()
    recorder = Recorder()
    feed.subscribe(recorder)
    churn = [Watchlist(f"w{i}", ["AAPL"]) for i in range(8)]

    def publish_fifty() -> None:
        for cents in range(50):
            feed.publish("AAPL", Money(100 + cents))

    def churn_subscriptions(watchlist: Watchlist) -> None:
        for _ in range(50):
            feed.subscribe(watchlist)
            feed.unsubscribe(watchlist)

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(publish_fifty) for _ in range(8)]
        futures += [pool.submit(churn_subscriptions, watchlist) for watchlist in churn]
        for future in futures:
            future.result()

    assert sorted(tick.seq for tick in recorder.ticks) == list(range(1, 401))
    assert feed.observer_count == 1  # every churned watchlist ended unsubscribed


def test_signal_calls_plain_callables_and_drops_weak_receivers_when_they_die() -> None:
    signal: Signal[PriceTick] = Signal()
    seen: list[int] = []
    signal.connect(lambda tick: seen.append(tick.seq))
    scratch = Watchlist("scratch", ["AAPL"])
    signal.connect(scratch.on_price, weak=True)
    assert len(signal) == 2
    assert signal.emit(PriceTick("AAPL", Money(1), 1)) == 2
    assert scratch.latest() == {"AAPL": Money(1)}

    del scratch
    gc.collect()
    assert signal.emit(PriceTick("AAPL", Money(2), 2)) == 1  # the dead weak receiver was pruned
    assert len(signal) == 1
    assert seen == [1, 2]


def test_signal_disconnect_removes_exactly_that_receiver() -> None:
    signal: Signal[int] = Signal()
    calls: list[str] = []

    def first(value: int) -> None:
        calls.append(f"first:{value}")

    def second(value: int) -> None:
        calls.append(f"second:{value}")

    signal.connect(first)
    signal.connect(second)
    signal.disconnect(first)
    assert signal.emit(7) == 1
    assert calls == ["second:7"]


def test_weak_bound_method_stays_connected_while_its_object_is_alive() -> None:
    signal: Signal[PriceTick] = Signal()
    watchlist = Watchlist("w", ["AAPL"])
    signal.connect(watchlist.on_price, weak=True)
    gc.collect()
    assert len(signal) == 1  # a plain weakref.ref to the bound method would already be dead
    signal.emit(PriceTick("AAPL", Money(5), 1))
    assert watchlist.latest() == {"AAPL": Money(5)}
