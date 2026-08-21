"""Observer: one-to-many change notification without the subject knowing who listens.

The running example is a market-data feed. ``PriceFeed`` (the Subject) publishes
``PriceTick`` values to whoever subscribed; ``Watchlist`` and ``PriceAlert`` (the
Observers) each react in their own way and the feed never learns what they do.
Two production details are built in: the subscriber list is guarded by a lock and
copied before dispatch, and an observer that raises can neither break the feed nor
starve the others. The last section restates the idea as a ``Signal`` of plain
callables with optional weak references, the Pythonic form.
"""

from __future__ import annotations

import inspect
import logging
import threading
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from common import Money

log = logging.getLogger(__name__)


# --8<-- [start:subject]
@dataclass(frozen=True, slots=True)
class PriceTick:
    """The event: an immutable value, so every observer may keep it without copying."""

    symbol: str
    price: Money
    seq: int


class PriceObserver(Protocol):
    """The Observer interface: one method, called on the publisher's thread."""

    def on_price(self, tick: PriceTick) -> None: ...


type ErrorHandler = Callable[[PriceObserver, PriceTick, Exception], None]


def log_observer_error(observer: PriceObserver, tick: PriceTick, exc: Exception) -> None:
    """Default error policy: record and move on. Inject another to count, alert or re-raise."""
    log.warning("observer %r failed on %s: %r", observer, tick, exc)


class PriceFeed:
    """The Subject: keeps the subscriber list and fans every tick out to it.

    ``_lock`` guards ``_observers`` and ``_seq``. ``publish`` copies the list under
    the lock and calls observers *outside* it, so a slow observer never blocks
    ``subscribe`` and an observer may unsubscribe itself from inside ``on_price``
    without corrupting the iteration. Each callback is isolated: an exception goes
    to ``on_error`` and the remaining observers still run.
    """

    def __init__(self, on_error: ErrorHandler = log_observer_error) -> None:
        self._observers: list[PriceObserver] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._on_error = on_error

    def subscribe(self, observer: PriceObserver) -> None:
        with self._lock:
            if not any(existing is observer for existing in self._observers):
                self._observers.append(observer)

    def unsubscribe(self, observer: PriceObserver) -> None:
        with self._lock:
            self._observers = [o for o in self._observers if o is not observer]

    @property
    def observer_count(self) -> int:
        with self._lock:
            return len(self._observers)

    def publish(self, symbol: str, price: Money) -> int:
        """Notify every current observer; returns how many were notified without error."""
        with self._lock:
            self._seq += 1
            tick = PriceTick(symbol, price, self._seq)
            observers = list(self._observers)
        delivered = 0
        for observer in observers:
            try:
                observer.on_price(tick)
                delivered += 1
            except Exception as exc:
                self._on_error(observer, tick, exc)
        return delivered


# --8<-- [end:subject]


# --8<-- [start:observers]
class Watchlist:
    """Keeps the latest price of the symbols it cares about and ignores the rest.

    Filtering is the observer's job: the feed stays a dumb fan-out. ``_lock`` guards
    ``_latest`` because ``on_price`` runs on whichever thread published.
    """

    def __init__(self, name: str, symbols: Iterable[str]) -> None:
        self.name = name
        self._symbols = frozenset(symbols)
        self._latest: dict[str, Money] = {}
        self._lock = threading.Lock()

    def on_price(self, tick: PriceTick) -> None:
        if tick.symbol in self._symbols:
            with self._lock:
                self._latest[tick.symbol] = tick.price

    def latest(self) -> dict[str, Money]:
        with self._lock:
            return dict(self._latest)


class PriceAlert:
    """Fires once when a symbol trades above a threshold, then stays quiet.

    It keeps receiving ticks until someone unsubscribes it: an observer that no
    longer cares but is still registered is the classic Observer leak.
    """

    def __init__(self, symbol: str, above: Money) -> None:
        self.symbol = symbol
        self.above = above
        self.fired_at: PriceTick | None = None

    def on_price(self, tick: PriceTick) -> None:
        if self.fired_at is None and tick.symbol == self.symbol and tick.price > self.above:
            self.fired_at = tick


# --8<-- [end:observers]


# --8<-- [start:signal]
def _strong[T](receiver: Callable[[T], None]) -> Callable[[], Callable[[T], None] | None]:
    return lambda: receiver


class Signal[T]:
    """The Pythonic observer: a list of callables, optionally held weakly.

    ``connect`` takes any callable (function, lambda, bound method). With
    ``weak=True`` the signal does not keep the receiver alive: once the last
    strong reference to a ``Watchlist`` is gone, its bound method is dropped on
    the next ``emit``. Bound methods need ``weakref.WeakMethod``; a plain
    ``weakref.ref`` to one dies immediately. Every slot is a zero-argument callable
    that returns the receiver or ``None``, so strong and weak receivers share one path.
    """

    def __init__(self) -> None:
        self._slots: list[Callable[[], Callable[[T], None] | None]] = []
        self._lock = threading.Lock()

    def connect(self, receiver: Callable[[T], None], *, weak: bool = False) -> None:
        slot: Callable[[], Callable[[T], None] | None]
        if not weak:
            slot = _strong(receiver)
        elif inspect.ismethod(receiver):
            slot = weakref.WeakMethod(receiver)
        else:
            slot = weakref.ref(receiver)
        with self._lock:
            self._slots.append(slot)

    def disconnect(self, receiver: Callable[[T], None]) -> None:
        with self._lock:
            self._slots = [slot for slot in self._slots if slot() != receiver]

    def emit(self, value: T) -> int:
        """Call every live receiver; dead weak receivers are pruned. Returns the call count."""
        with self._lock:
            resolved = [(slot, slot()) for slot in self._slots]
            self._slots = [slot for slot, target in resolved if target is not None]
            receivers = [target for _, target in resolved if target is not None]
        for receiver in receivers:
            receiver(value)
        return len(receivers)

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for slot in self._slots if slot() is not None)


# --8<-- [end:signal]


def main() -> None:
    class BrokenObserver:
        def on_price(self, tick: PriceTick) -> None:
            raise RuntimeError("downstream store unavailable")

    failures: list[str] = []
    feed = PriceFeed(on_error=lambda obs, tick, exc: failures.append(f"seq {tick.seq}: {exc}"))
    tech = Watchlist("tech", ["AAPL", "MSFT"])
    energy = Watchlist("energy", ["XOM"])
    alert = PriceAlert("AAPL", above=Money.of("200.00"))
    for observer in (tech, energy, alert, BrokenObserver()):
        feed.subscribe(observer)
    print(f"--- {feed.observer_count} observers subscribed; one of them always raises ---")
    for symbol, price in [("AAPL", "199.50"), ("XOM", "105.10"), ("AAPL", "201.25"), ("MSFT", "410.00")]:
        delivered = feed.publish(symbol, Money.of(price))
        print(f"tick {symbol:<4} at {price:>7}: delivered to {delivered} of {feed.observer_count}")
    print(f"tech watchlist:    {', '.join(f'{s}={p}' for s, p in sorted(tech.latest().items()))}")
    print(f"energy watchlist:  {', '.join(f'{s}={p}' for s, p in sorted(energy.latest().items()))}")
    assert alert.fired_at is not None
    print(f"alert fired:       seq {alert.fired_at.seq} at {alert.fired_at.price}")
    print(f"isolated failures: {len(failures)} (first: {failures[0]})")
    feed.unsubscribe(alert)
    print(f"alert unsubscribed -> {feed.observer_count} observers remain")

    print("--- Signal: the same fan-out with plain callables and a weak receiver ---")
    signal: Signal[PriceTick] = Signal()
    seen: list[str] = []
    signal.connect(lambda tick: seen.append(f"{tick.symbol}@{tick.price}"))
    scratch = Watchlist("scratch", ["AAPL"])
    signal.connect(scratch.on_price, weak=True)
    print(f"receivers while the scratch watchlist is alive: {len(signal)}")
    del scratch
    called = signal.emit(PriceTick("AAPL", Money.of("202.00"), 5))
    print(f"receivers after it was garbage-collected:      {len(signal)} (emit reached {called})")
    print(f"lambda receiver saw: {seen}")


if __name__ == "__main__":
    main()
