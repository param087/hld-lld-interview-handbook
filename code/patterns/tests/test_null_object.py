"""Null Object: the do-nothing implementation keeps the client on one code path."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext

import pytest

from common import Money, SequentialIdGenerator, ValidationError
from patterns.null_object import (
    NO_DISCOUNT,
    NO_LOCK,
    NO_NOTIFICATIONS,
    Checkout,
    DiscountPolicy,
    NoDiscount,
    Notifier,
    NullNotifier,
    PercentageDiscount,
    RecordingNotifier,
    library_logger,
    optional_lock,
)

CART = [Money.of("12.00"), Money.of("18.00")]


def test_null_objects_satisfy_the_protocols_by_shape_and_compare_equal() -> None:
    assert isinstance(NullNotifier(), Notifier) and Notifier not in NullNotifier.__mro__
    assert isinstance(NoDiscount(), DiscountPolicy) and DiscountPolicy not in NoDiscount.__mro__
    assert isinstance(NO_LOCK, AbstractContextManager)
    assert isinstance(threading.Lock(), AbstractContextManager)
    assert NullNotifier() == NO_NOTIFICATIONS and NoDiscount() == NO_DISCOUNT
    assert len({NullNotifier(), NullNotifier(), NoDiscount()}) == 2  # hashable values


@pytest.mark.parametrize("subtotal", [Money.of("0.00"), Money.of("30.00"), Money.of("99.99", "EUR")])
def test_no_discount_is_the_identity_and_null_notifier_is_silent(subtotal: Money) -> None:
    assert NoDiscount().discount(subtotal) == Money(0, subtotal.currency)
    assert subtotal - NoDiscount().discount(subtotal) == subtotal
    assert NullNotifier().send("anyone", "anything") is None


def test_checkout_has_one_code_path_whatever_it_is_given() -> None:
    guest = Checkout(SequentialIdGenerator("order"))
    plain = guest.place("guest", CART)
    assert (plain.subtotal, plain.discount, plain.total) == (Money.of("30.00"), Money(0), Money.of("30.00"))
    assert guest.placed == 1

    inbox = RecordingNotifier()
    member = Checkout(SequentialIdGenerator("order"), inbox, PercentageDiscount(10))
    deal = member.place("grace", CART)
    assert (deal.subtotal, deal.discount, deal.total) == (Money.of("30.00"), Money.of("3.00"), Money.of("27.00"))
    assert inbox.sent == [("grace", "order order-1: 27.00 USD")]


@pytest.mark.parametrize(
    ("percent", "subtotal", "expected"),
    [
        (10, "30.00", "3.00"),
        (15, "10.01", "1.50"),  # 1.5015 rounds down
        (7, "0.50", "0.04"),  # 0.035 rounds half-up
        (100, "5.00", "5.00"),
    ],
)
def test_percentage_discount_rounds_to_the_cent(percent: int, subtotal: str, expected: str) -> None:
    assert PercentageDiscount(percent).discount(Money.of(subtotal)) == Money.of(expected)


@pytest.mark.parametrize("percent", [0, -5, 101])
def test_percentage_discount_rejects_nonsense(percent: int) -> None:
    with pytest.raises(ValidationError):
        PercentageDiscount(percent)


def test_null_collaborators_do_not_swallow_validation() -> None:
    shop = Checkout(SequentialIdGenerator("order"))
    with pytest.raises(ValidationError):
        shop.place("guest", [])
    assert shop.placed == 0


def test_nullcontext_and_a_real_lock_are_interchangeable_in_the_client() -> None:
    assert isinstance(optional_lock(False), nullcontext)
    assert not isinstance(optional_lock(True), nullcontext)

    threaded = Checkout(SequentialIdGenerator("order"), lock=optional_lock(True))

    def place(n: int) -> str:
        return threaded.place(f"customer-{n}", [Money.of("1.00")]).order_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(place, range(200)))
    assert len(set(ids)) == 200 and threaded.placed == 200

    single = Checkout(SequentialIdGenerator("order"), lock=optional_lock(False))
    for n in range(50):
        single.place(f"customer-{n}", [Money.of("1.00")])
    assert single.placed == 50


def test_null_handler_is_the_difference_between_silence_and_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    assert [type(h).__name__ for h in library_logger("handbook.tests.lib").handlers] == ["NullHandler"]

    noisy = logging.Logger("noisy")  # detached from the hierarchy: no handler anywhere above it
    noisy.warning("nobody configured logging")
    quiet = logging.Logger("quiet")
    quiet.addHandler(logging.NullHandler())
    quiet.warning("nobody configured logging")
    assert capsys.readouterr().err.count("nobody configured logging") == 1  # only the noisy one
