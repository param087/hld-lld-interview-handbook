from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, Money, SequentialIdGenerator
from lld.parking_lot.models import (
    InvalidTicketError,
    LotFullError,
    PaymentDeclinedError,
    PaymentMethod,
    SpotType,
    TicketStateError,
    TicketStatus,
    VehicleType,
)
from lld.parking_lot.services import DisplayBoard, EntryGate, ExitGate, ParkingFloor, ParkingLot
from lld.parking_lot.strategies import DailyCapPricing, FlatRatePricing, HourlyPricing


def make_lot(clock: FakeClock, layouts: list[dict[SpotType, int]]) -> ParkingLot:
    floors = [ParkingFloor.build(i + 1, layout) for i, layout in enumerate(layouts)]
    return ParkingLot("test", floors, clock=clock, ids=SequentialIdGenerator("T"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000)


def test_park_then_exit_charges_per_started_hour(clock: FakeClock) -> None:
    lot = make_lot(clock, [{SpotType.COMPACT: 2}])
    entry, exit_gate = EntryGate("in", lot), ExitGate("out", lot, clock=clock)
    ticket = entry.issue_ticket("car", "ka01ab1234")
    assert ticket.plate == "KA01AB1234" and ticket.spot_id == "F1-C01"
    clock.advance(2 * 3600 + 35 * 60)
    payment = exit_gate.process(ticket.id, PaymentMethod.CARD)
    assert payment.amount == Money.of("9.00")  # 3 started hours x $3
    assert ticket.status is TicketStatus.PAID and lot.free_spots() == 2


def test_grace_period_is_free(clock: FakeClock) -> None:
    lot = make_lot(clock, [{SpotType.COMPACT: 1}])
    ticket = EntryGate("in", lot).issue_ticket("car", "X1")
    clock.advance(10 * 60)
    assert ExitGate("out", lot, clock=clock).quote(ticket.id) == Money(0)


def test_motorcycle_prefers_motorcycle_spot_then_falls_back(clock: FakeClock) -> None:
    lot = make_lot(clock, [{SpotType.MOTORCYCLE: 1, SpotType.COMPACT: 1}])
    entry = EntryGate("in", lot)
    first = entry.issue_ticket(VehicleType.MOTORCYCLE, "M1")
    second = entry.issue_ticket(VehicleType.MOTORCYCLE, "M2")
    assert (first.spot_id, second.spot_id) == ("F1-M01", "F1-C01")
    with pytest.raises(LotFullError):
        entry.issue_ticket(VehicleType.MOTORCYCLE, "M3")


def test_truck_only_fits_large_spots_and_spills_to_next_floor(clock: FakeClock) -> None:
    lot = make_lot(clock, [{SpotType.COMPACT: 5}, {SpotType.LARGE: 1}])
    entry = EntryGate("in", lot)
    assert entry.issue_ticket("truck", "T1").spot_id == "F2-L01"
    with pytest.raises(LotFullError):
        entry.issue_ticket("truck", "T2")
    assert entry.issue_ticket("car", "C1").floor == 1


# --8<-- [start:concurrency]
def test_concurrent_gates_never_double_assign_a_spot(clock: FakeClock) -> None:
    lot = make_lot(clock, [{SpotType.COMPACT: 5}, {SpotType.COMPACT: 5}])
    gates = [EntryGate(f"in-{i}", lot) for i in range(3)]

    def arrive(i: int) -> str | None:
        try:
            return gates[i % 3].issue_ticket("car", f"CAR{i}").spot_id
        except LotFullError:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(arrive, range(40)))
    spots = [s for s in results if s is not None]
    assert len(spots) == 10 and len(set(spots)) == 10  # every spot used exactly once
    assert results.count(None) == 30 and lot.free_spots() == 0


# --8<-- [end:concurrency]


def test_exit_twice_is_rejected_and_unknown_ticket_raises(clock: FakeClock) -> None:
    lot = make_lot(clock, [{SpotType.COMPACT: 1}])
    ticket = EntryGate("in", lot).issue_ticket("car", "C1")
    exit_gate = ExitGate("out", lot, clock=clock)
    exit_gate.process(ticket.id, PaymentMethod.CASH)
    with pytest.raises(TicketStateError):
        exit_gate.process(ticket.id, PaymentMethod.CASH)
    with pytest.raises(InvalidTicketError):
        exit_gate.process("nope", PaymentMethod.CASH)


# --8<-- [start:declined]
def test_declined_payment_keeps_the_car_parked(clock: FakeClock) -> None:
    class Declines:
        def charge(self, amount: Money, method: PaymentMethod) -> bool:
            return False

    lot = make_lot(clock, [{SpotType.COMPACT: 1}])
    ticket = EntryGate("in", lot).issue_ticket("car", "C1")
    clock.advance(3600)
    with pytest.raises(PaymentDeclinedError):
        ExitGate("out", lot, processor=Declines(), clock=clock).process(ticket.id, PaymentMethod.CARD)
    assert ticket.status is TicketStatus.ACTIVE and lot.free_spots() == 0


# --8<-- [end:declined]


def test_lost_ticket_charges_flat_fee_and_frees_the_spot(clock: FakeClock) -> None:
    lot = make_lot(clock, [{SpotType.COMPACT: 1}])
    EntryGate("in", lot).issue_ticket("car", "C1")
    payment = ExitGate("out", lot, clock=clock).process_lost_ticket("c1", PaymentMethod.CASH)
    assert payment.amount == ExitGate.LOST_TICKET_FEE and lot.free_spots() == 1


def test_display_board_is_updated_on_assign_and_release(clock: FakeClock) -> None:
    lot = make_lot(clock, [{SpotType.COMPACT: 2, SpotType.LARGE: 1}])
    board = DisplayBoard()
    lot.floors()[0].subscribe(board)
    assert board.free_spots(1) == 3
    ticket = EntryGate("in", lot).issue_ticket("car", "C1")
    assert board.free_spots(1) == 2 and "1 compact, 1 large" in board.render()
    ExitGate("out", lot, clock=clock).process(ticket.id, PaymentMethod.CASH)
    assert board.free_spots(1) == 3


@pytest.mark.parametrize(
    ("strategy", "seconds", "expected"),
    [
        (HourlyPricing(), 59 * 60, "3.00"),
        (HourlyPricing(), 61 * 60, "6.00"),
        (FlatRatePricing(Money.of("10.00")), 5 * 3600, "10.00"),
        (DailyCapPricing(HourlyPricing(), Money.of("20.00")), 10 * 3600, "20.00"),
        (DailyCapPricing(HourlyPricing(), Money.of("20.00")), 26 * 3600, "26.00"),
    ],
)
def test_pricing_strategies(strategy, seconds: int, expected: str) -> None:
    assert strategy.calculate(VehicleType.CAR, seconds) == Money.of(expected)
