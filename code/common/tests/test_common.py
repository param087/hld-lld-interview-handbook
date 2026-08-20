from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from decimal import Decimal

import pytest

from common import (
    ConflictError,
    FakeClock,
    HandbookError,
    Money,
    SequentialIdGenerator,
    SystemClock,
    UuidIdGenerator,
)


def test_fake_clock_advances_and_sets() -> None:
    clock = FakeClock(start=100.0)
    clock.advance(5)
    assert clock.now() == 105.0
    clock.set(7.0)
    assert clock.now() == 7.0
    assert clock.now_dt().tzinfo is UTC
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_system_clock_is_monotonic_enough() -> None:
    clock = SystemClock()
    a = clock.now()
    b = clock.now()
    assert b >= a
    assert clock.now_dt().tzinfo is UTC


def test_sequential_ids_are_unique_under_threads() -> None:
    gen = SequentialIdGenerator(prefix="t")
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: gen.next_id(), range(2000)))
    assert len(set(ids)) == 2000
    assert ids[0].startswith("t-")


def test_uuid_ids_are_unique() -> None:
    gen = UuidIdGenerator()
    assert len({gen.next_id() for _ in range(100)}) == 100


@pytest.mark.parametrize(
    ("amount", "cents"),
    [("12.34", 1234), ("0.005", 1), (3, 300), (Decimal("1.999"), 200)],
)
def test_money_of_rounds_half_up(amount: object, cents: int) -> None:
    assert Money.of(amount).cents == cents  # type: ignore[arg-type]


def test_money_arithmetic_and_currency_guard() -> None:
    a, b = Money(150), Money(50)
    assert (a + b).cents == 200
    assert (a - b).cents == 100
    assert (a * 2).cents == 300
    assert (-a).cents == -150
    assert str(Money(-1205)) == "-12.05 USD"
    with pytest.raises(ValueError):
        _ = a + Money(1, "EUR")


def test_money_allocate_never_loses_cents() -> None:
    parts = Money(100).allocate([1, 1, 1])
    assert [p.cents for p in parts] == [34, 33, 33]
    assert sum(p.cents for p in Money(1001).allocate([3, 7])) == 1001
    with pytest.raises(ValueError):
        Money(1).allocate([])


def test_error_hierarchy() -> None:
    assert issubclass(ConflictError, HandbookError)
    with pytest.raises(HandbookError):
        raise ConflictError("double booking")
