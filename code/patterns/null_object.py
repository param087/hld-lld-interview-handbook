"""Null Object: a do-nothing implementation that honours the interface, so callers drop their None checks.

The running example is checkout. ``Checkout`` needs a ``Notifier`` and a ``DiscountPolicy``;
for a guest without contact details and a cart without a coupon it gets ``NullNotifier``
and ``NoDiscount``, which satisfy the same Protocols and do nothing, instead of ``None``
and an ``if`` at every call site. The last section shows the stdlib's own null objects:
``contextlib.nullcontext`` standing in for a lock and ``logging.NullHandler`` standing in
for a handler.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

from common import IdGenerator, Money, SequentialIdGenerator, ValidationError


# --8<-- [start:notifier]
@runtime_checkable
class Notifier(Protocol):
    def send(self, recipient: str, message: str) -> None: ...


class RecordingNotifier:
    """A working implementation: keeps what was sent (the demo's stand-in for email)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, recipient: str, message: str) -> None:
        self.sent.append((recipient, message))


@dataclass(frozen=True, slots=True)
class NullNotifier:
    """The null object: same contract, no behaviour, no failure modes, no state.

    Frozen and field-less, so every ``NullNotifier()`` equals every other and a single
    instance can be shared by every caller in the process.
    """

    def send(self, recipient: str, message: str) -> None:
        return None


# --8<-- [end:notifier]


# --8<-- [start:discount]
@runtime_checkable
class DiscountPolicy(Protocol):
    def discount(self, subtotal: Money) -> Money: ...  # the amount to subtract


@dataclass(frozen=True, slots=True)
class NoDiscount:
    """The identity element of discounts: subtracts zero, so the caller never branches."""

    def discount(self, subtotal: Money) -> Money:
        return Money(0, subtotal.currency)


@dataclass(frozen=True, slots=True)
class PercentageDiscount:
    percent: int

    def __post_init__(self) -> None:
        if not 0 < self.percent <= 100:
            raise ValidationError(f"percent must be between 1 and 100, got {self.percent}")

    def discount(self, subtotal: Money) -> Money:
        return subtotal * (Decimal(self.percent) / 100)  # Money rounds half-up to the cent


# --8<-- [end:discount]


# --8<-- [start:checkout]
@dataclass(frozen=True, slots=True)
class Receipt:
    order_id: str
    customer_id: str
    subtotal: Money
    discount: Money
    total: Money


# Null objects carry no state, so one shared instance per type is enough, and because
# they are immutable they are safe as default arguments.
NO_DISCOUNT: Final = NoDiscount()
NO_NOTIFICATIONS: Final = NullNotifier()
NO_LOCK: Final = nullcontext()  # the stdlib's null object for "a context manager"


class Checkout:
    """The client. It never asks whether a notifier, a coupon or a lock is present.

    Each optional collaborator defaults to its null object, so ``place`` has one code
    path. ``_lock`` protects ``_receipts``: a single-threaded caller keeps the
    ``nullcontext`` default, a threaded one passes ``threading.Lock()``.
    """

    def __init__(
        self,
        ids: IdGenerator,
        notifier: Notifier = NO_NOTIFICATIONS,
        discount: DiscountPolicy = NO_DISCOUNT,
        lock: AbstractContextManager[object] = NO_LOCK,
    ) -> None:
        self._ids = ids
        self._notifier = notifier
        self._discount = discount
        self._lock = lock
        self._receipts: list[Receipt] = []

    def place(self, customer_id: str, prices: Sequence[Money]) -> Receipt:
        if not prices:
            raise ValidationError("an order needs at least one item")
        subtotal = sum(prices[1:], start=prices[0])
        off = self._discount.discount(subtotal)
        receipt = Receipt(self._ids.next_id(), customer_id, subtotal, off, subtotal - off)
        with self._lock:
            self._receipts.append(receipt)
        self._notifier.send(customer_id, f"order {receipt.order_id}: {receipt.total}")
        return receipt

    @property
    def placed(self) -> int:
        with self._lock:
            return len(self._receipts)


# --8<-- [end:checkout]


# --8<-- [start:stdlib]
def library_logger(name: str) -> logging.Logger:
    """What a library does at import time: attach ``NullHandler`` so it never writes to stderr.

    When no handler exists anywhere in a logger's hierarchy, ``logging`` falls back to
    ``lastResort`` and prints WARNING and above to stderr. ``NullHandler`` counts as a
    handler and does nothing, so the fallback never fires and the application that
    imports the library stays in charge of where log output goes.
    """
    logger = logging.getLogger(name)
    logger.addHandler(logging.NullHandler())
    return logger


def optional_lock(thread_safe: bool) -> AbstractContextManager[object]:
    """``nullcontext()`` is the null object for locks: the same ``with``, no locking."""
    return threading.Lock() if thread_safe else nullcontext()


# --8<-- [end:stdlib]


def place_many(shop: Checkout, count: int, workers: int) -> int:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(lambda n: shop.place(f"customer-{n}", [Money.of("1.00")]), range(count)):
            pass
    return shop.placed


def main() -> None:
    print("--- a guest checkout: no coupon, no contact details, one thread ---")
    guest = Checkout(SequentialIdGenerator("order"))  # every optional collaborator is a null object
    receipt = guest.place("guest", [Money.of("12.00"), Money.of("18.00")])
    print(f"{receipt.order_id}: subtotal {receipt.subtotal}, discount {receipt.discount}, total {receipt.total}")

    print("--- a member checkout: a 10% coupon and an email on file ---")
    inbox = RecordingNotifier()
    member = Checkout(SequentialIdGenerator("order", start=2), inbox, PercentageDiscount(10))
    receipt = member.place("grace", [Money.of("12.00"), Money.of("18.00")])
    print(f"{receipt.order_id}: subtotal {receipt.subtotal}, discount {receipt.discount}, total {receipt.total}")
    print(f"notified: {inbox.sent[-1]}")

    print("--- the same Checkout code path, with and without a real lock ---")
    for label, thread_safe, workers in (("nullcontext, one thread", False, 1), ("threading.Lock, four threads", True, 4)):
        shop = Checkout(SequentialIdGenerator("order"), lock=optional_lock(thread_safe))
        print(f"{label}: {place_many(shop, 100, workers)} orders placed")

    print("--- logging.NullHandler: the stdlib's null object for handlers ---")
    lib = library_logger("handbook.lib")
    lib.warning("a library warning with nobody configured to listen")
    print(f"handlers on the library logger: {[type(h).__name__ for h in lib.handlers]}; the warning went nowhere")

    print("--- null objects are values: one shared instance is enough ---")
    print(f"NullNotifier() == NullNotifier(): {NullNotifier() == NullNotifier()}; NoDiscount() == NO_DISCOUNT: {NoDiscount() == NO_DISCOUNT}")


if __name__ == "__main__":
    main()
