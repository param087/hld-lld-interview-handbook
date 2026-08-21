"""Contracts: the promises this service makes, asserted one by one."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, NotFoundError, ValidationError
from fundamentals.contracts import (
    InMemoryReservationLog,
    InMemoryStockRepository,
    Page,
    Rejected,
    RejectionReason,
    ReservationService,
    ReservationView,
    Reserved,
    ReserveStock,
    SortableIdGenerator,
    StockItem,
    StockRepository,
    build_service,
)


def a_service(on_hand: dict[str, int] | None = None):
    return build_service(on_hand or {"SKU-A": 10, "SKU-B": 1}, FakeClock(start=1_000.0))


@pytest.mark.parametrize(
    ("sku", "quantity", "order_id", "key"),
    [
        ("SKU-A", 0, "ORD-1", "key-1"),
        ("SKU-A", -3, "ORD-1", "key-1"),
        ("", 1, "ORD-1", "key-1"),
        ("SKU-A", 1, "", "key-1"),
        ("SKU-A", 1, "ORD-1", ""),
    ],
)
def test_the_command_dto_refuses_to_exist_when_its_contract_is_broken(
    sku: str, quantity: int, order_id: str, key: str
) -> None:
    with pytest.raises(ValidationError):
        ReserveStock(sku, quantity, order_id, key)


def test_an_expected_failure_is_a_value_and_a_broken_precondition_is_an_exception() -> None:
    service = a_service()
    rejected = service.reserve(ReserveStock("SKU-B", 5, "ORD-1", "key-1"))
    assert rejected == Rejected("SKU-B", RejectionReason.OUT_OF_STOCK, available=1)
    with pytest.raises(NotFoundError):
        service.reserve(ReserveStock("SKU-NONE", 1, "ORD-2", "key-2"))


def test_a_withdrawn_item_is_rejected_with_the_reason_the_caller_can_act_on() -> None:
    repository = InMemoryStockRepository({"SKU-X": StockItem("SKU-X", 5, withdrawn=True)})
    service = ReservationService(
        repository, InMemoryReservationLog(), SortableIdGenerator(), FakeClock()
    )
    rejected = service.reserve(ReserveStock("SKU-X", 1, "ORD-1", "key-1"))
    assert isinstance(rejected, Rejected) and rejected.reason is RejectionReason.ITEM_WITHDRAWN
    assert repository.get("SKU-X").reserved == 0  # rejected means nothing moved


def test_replaying_one_idempotency_key_takes_stock_once() -> None:
    service = a_service()
    command = ReserveStock("SKU-A", 4, "ORD-1", "key-1")
    first = service.reserve(command)
    assert service.reserve(command) is first
    assert isinstance(first, Reserved)
    remaining = service.reserve(ReserveStock("SKU-A", 6, "ORD-2", "key-2"))
    assert isinstance(remaining, Reserved)  # 4 + 6 = 10, so the first call ran exactly once


def test_concurrent_calls_never_oversell_and_replays_still_dedupe() -> None:
    service = a_service({"SKU-A": 10})
    commands = [ReserveStock("SKU-A", 1, f"ORD-{i}", f"key-{i % 30}") for i in range(60)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(service.reserve, commands))
    granted = {r.reservation_id for r in results if isinstance(r, Reserved)}
    assert len(granted) == 10  # exactly the stock, never more
    assert all(isinstance(r, Rejected) for r in results if not isinstance(r, Reserved))


def test_the_invariant_survives_reserve_and_release_and_guards_the_edges() -> None:
    item = StockItem("SKU-A", on_hand=5)
    item.reserve(3)
    assert (item.reserved, item.available) == (3, 2)
    item.release(3)
    assert (item.reserved, item.available) == (0, 5)
    for bad in (0, -1, 6):
        with pytest.raises(ValidationError):
            item.reserve(bad)
    with pytest.raises(ValidationError):
        item.release(1)  # nothing is reserved
    assert 0 <= item.reserved <= item.on_hand


def test_keyset_pagination_walks_every_row_exactly_once() -> None:
    service = a_service({"SKU-A": 10})
    for index in range(5):
        service.reserve(ReserveStock("SKU-A", 1, f"ORD-{index}", f"key-{index}"))
    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = service.reservations_for("SKU-A", limit=2, cursor=cursor)
        seen += [row.reservation_id for row in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == [f"RES-{n:04d}" for n in range(1, 6)]
    assert len(set(seen)) == len(seen)


def test_a_row_written_before_the_cursor_does_not_shift_the_next_page() -> None:
    log = InMemoryReservationLog()
    for n in range(1, 6):
        log.append(Reserved(f"RES-{n:04d}", "SKU-A", 1, 0.0))
    first = log.page_for_sku("SKU-A", limit=2)
    assert [row.reservation_id for row in first.items] == ["RES-0001", "RES-0002"]
    log.append(Reserved("RES-0000", "SKU-A", 1, 0.0))  # a late write that sorts first
    second = log.page_for_sku("SKU-A", limit=2, cursor=first.next_cursor)
    assert [row.reservation_id for row in second.items] == ["RES-0003", "RES-0004"]
    # OFFSET 2 would have returned RES-0002 a second time and skipped nothing else


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_the_page_size_is_part_of_the_contract(limit: int) -> None:
    with pytest.raises(ValidationError):
        InMemoryReservationLog().page_for_sku("SKU-A", limit=limit)


def test_an_empty_log_returns_an_empty_terminal_page() -> None:
    assert InMemoryReservationLog().page_for_sku("SKU-A") == Page((), None)


def test_the_view_is_additive_so_an_older_client_sees_what_it_always_saw() -> None:
    reserved = Reserved("RES-0001", "SKU-A", 3, 1_900.0)
    v1_shaped = ReservationView.of(reserved).to_payload()
    v2 = ReservationView.of(reserved, warehouse="LON-1").to_payload()
    assert "warehouse" not in v1_shaped  # unset optional fields are omitted, not null
    assert v2["warehouse"] == "LON-1"
    assert v1_shaped.items() <= v2.items()  # v2 only adds keys; none changed meaning
    assert v2["schema_version"] == 2


def test_the_repository_protocol_is_satisfied_by_shape_alone() -> None:
    class RecordingStock:
        def __init__(self) -> None:
            self.saved: list[str] = []

        def get(self, sku: str) -> StockItem:
            return StockItem(sku, 1)

        def save(self, item: StockItem) -> None:
            self.saved.append(item.sku)

    assert isinstance(RecordingStock(), StockRepository)
    assert StockRepository not in RecordingStock.__mro__
    assert not isinstance(object(), StockRepository)
