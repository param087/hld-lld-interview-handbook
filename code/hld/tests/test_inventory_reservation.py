from concurrent.futures import ThreadPoolExecutor

import pytest

from common import (
    ConflictError,
    FakeClock,
    InvalidStateError,
    Money,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)
from hld.inventory_reservation import (
    FlashSaleCounter,
    InventoryService,
    Outbox,
    ReservationState,
    build_checkout_saga,
)
from hld.saga import SagaLog, SagaState, StepFailed


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000.0)


@pytest.fixture
def inventory(clock: FakeClock) -> InventoryService:
    service = InventoryService(clock, SequentialIdGenerator("rsv"), hold_ttl=900)
    for sku, qty in (("tshirt", 5), ("mug", 2), ("poster", 10)):
        service.stock_in(sku, qty)
    return service


def test_reserve_takes_every_line_or_none_and_bumps_versions(inventory: InventoryService) -> None:
    reservation = inventory.reserve("ord-1", {"tshirt": 2, "mug": 1})
    assert reservation.state is ReservationState.HELD
    assert inventory.snapshot()["tshirt"] == (3, 2)
    versions = inventory.versions("tshirt", "mug")
    with pytest.raises(ConflictError, match="insufficient stock"):
        inventory.reserve("ord-2", {"tshirt": 1, "mug": 2})
    assert inventory.snapshot()["tshirt"] == (3, 2)  # the failed line left nothing behind
    assert inventory.versions("tshirt", "mug") == versions
    assert inventory.commit(reservation.reservation_id).state is ReservationState.COMMITTED
    assert inventory.snapshot()["tshirt"] == (3, 0)  # units left the warehouse


def test_reserve_is_idempotent_per_order(inventory: InventoryService) -> None:
    first = inventory.reserve("ord-1", {"poster": 4})
    second = inventory.reserve("ord-1", {"poster": 4})
    assert first is second
    assert inventory.available("poster") == 6  # reserved once, not twice


def test_expected_versions_make_reserve_a_compare_and_set(inventory: InventoryService) -> None:
    seen = inventory.versions("poster")
    inventory.stock_in("poster", 5)  # a warehouse receipt lands between the read and the write
    with pytest.raises(ConflictError, match="stale stock version"):
        inventory.reserve("ord-1", {"poster": 1}, expected_versions=seen)
    assert inventory.available("poster") == 15
    inventory.reserve("ord-1", {"poster": 1}, expected_versions=inventory.versions("poster"))
    assert inventory.available("poster") == 14


def test_expired_reservation_is_reclaimed_lazily_and_cannot_be_committed(
    inventory: InventoryService, clock: FakeClock
) -> None:
    stale = inventory.reserve("ord-1", {"mug": 2})
    assert inventory.available("mug") == 0
    clock.advance(901)
    other = inventory.reserve("ord-2", {"mug": 2})  # lazy reclaim, no sweeper needed
    assert other.reservation_id != stale.reservation_id
    assert stale.state is ReservationState.EXPIRED
    with pytest.raises(InvalidStateError, match="expired"):
        inventory.commit(stale.reservation_id)
    assert inventory.commit(other.reservation_id).state is ReservationState.COMMITTED


def test_release_is_idempotent_and_a_committed_reservation_is_terminal(
    inventory: InventoryService, clock: FakeClock
) -> None:
    reservation = inventory.reserve("ord-1", {"poster": 3})
    inventory.release(reservation.reservation_id)
    inventory.release(reservation.reservation_id)  # a saga compensation may run twice
    assert inventory.available("poster") == 10
    with pytest.raises(InvalidStateError, match="released"):
        inventory.commit(reservation.reservation_id)
    sold = inventory.reserve("ord-2", {"poster": 3})
    inventory.commit(sold.reservation_id)
    assert inventory.commit(sold.reservation_id).state is ReservationState.COMMITTED  # retried step
    with pytest.raises(InvalidStateError, match="already committed"):
        inventory.release(sold.reservation_id)
    clock.advance(901)
    assert inventory.expire() == 0  # nothing is still held


def test_validation_and_unknown_entities(inventory: InventoryService, clock: FakeClock) -> None:
    with pytest.raises(ValidationError):
        inventory.reserve("ord-1", {})
    with pytest.raises(ValidationError):
        inventory.reserve("ord-1", {"mug": 0})
    with pytest.raises(NotFoundError, match="unknown skus"):
        inventory.reserve("ord-1", {"kettle": 1})
    with pytest.raises(NotFoundError):
        inventory.commit("rsv-999")
    with pytest.raises(ValidationError):
        InventoryService(clock, hold_ttl=0)
    with pytest.raises(ValidationError):
        inventory.stock_in("mug", -1)


def test_checkout_saga_completes_and_the_outbox_carries_every_event(
    inventory: InventoryService,
) -> None:
    outbox, log = Outbox(), SagaLog()
    saga = build_checkout_saga(
        inventory, outbox, lambda order_id, amount: f"pay-{order_id}", lambda ref: None, log
    )
    assert saga.start("ord-1", {"order_id": "ord-1", "lines": {"poster": 3}, "amount": Money.of("29.97")}) is SagaState.COMPLETED
    context = log.get("ord-1").context
    assert context["payment_ref"] == "pay-ord-1"
    assert context["shipment_id"] == "shp-ord-1"
    assert [event.topic for event in outbox.relay()] == [
        "inventory-reserved",
        "payment-captured",
        "inventory-committed",
        "shipment-created",
    ]
    assert inventory.available("poster") == 7
    assert inventory.snapshot()["poster"] == (7, 0)


def test_checkout_saga_compensates_when_the_card_is_declined(inventory: InventoryService) -> None:
    outbox = Outbox()
    refunds: list[str] = []

    def declined(order_id: str, amount: Money) -> str:
        raise StepFailed(f"card declined for {amount}")

    saga = build_checkout_saga(inventory, outbox, declined, refunds.append)
    context = {"order_id": "ord-1", "lines": {"poster": 4}, "amount": Money.of("39.96")}
    assert saga.start("ord-1", context) is SagaState.COMPENSATED  # the pivot failed
    assert inventory.available("poster") == 10  # the reservation was released
    assert refunds == []  # nothing was captured, so nothing to refund
    assert [event.topic for event in outbox.relay()] == ["inventory-reserved", "order-cancelled"]


def test_checkout_saga_fails_fast_when_stock_is_short(inventory: InventoryService) -> None:
    outbox, log = Outbox(), SagaLog()
    saga = build_checkout_saga(inventory, outbox, lambda order_id, amount: "pay-x", lambda ref: None, log)
    context = {"order_id": "ord-1", "lines": {"mug": 99}, "amount": Money.of("9.99")}
    assert saga.start("ord-1", context) is SagaState.COMPENSATED
    assert "payment_ref" not in log.get("ord-1").context  # the pivot was never reached
    assert log.get("ord-1").steps_with("failed") == ["reserve_inventory"]  # business failure: no retry
    assert inventory.available("mug") == 2


def test_flash_sale_counter_spreads_across_shards_and_sells_out_exactly_once() -> None:
    drop = FlashSaleCounter("console", total=100, shards=8)
    claimed = [drop.take() for _ in range(100)]
    assert all(index is not None for index in claimed)
    assert sorted({index for index in claimed if index is not None}) == list(range(8))
    assert drop.remaining() == 0
    assert drop.take() is None
    drop.give_back(3)
    assert drop.remaining() == 1 and drop.take() == 3
    with pytest.raises(ValidationError):
        FlashSaleCounter("x", total=0)


def test_concurrent_reservations_never_oversell(clock: FakeClock) -> None:
    inventory = InventoryService(clock, SequentialIdGenerator("rsv"), hold_ttl=900)
    inventory.stock_in("gpu", 10)

    def buy(i: int) -> bool:
        try:
            inventory.reserve(f"ord-{i}", {"gpu": 1})
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(buy, range(200)))
    assert results.count(True) == 10
    assert inventory.available("gpu") == 0
    assert inventory.snapshot()["gpu"] == (0, 10)


def test_concurrent_flash_sale_take_hands_out_each_unit_once() -> None:
    drop = FlashSaleCounter("console", total=500, shards=16)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: drop.take(), range(1_000)))
    assert len([index for index in outcomes if index is not None]) == 500
    assert drop.remaining() == 0
