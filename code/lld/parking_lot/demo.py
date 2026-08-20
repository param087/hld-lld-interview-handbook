"""A short scenario: two floors, four vehicles, one exit, one lost ticket."""

from common import FakeClock, SequentialIdGenerator
from lld.parking_lot.models import PaymentMethod, SpotType, TicketStateError
from lld.parking_lot.services import DisplayBoard, EntryGate, ExitGate, ParkingFloor, ParkingLot
from lld.parking_lot.strategies import HourlyPricing


def build_lot(clock: FakeClock) -> tuple[ParkingLot, DisplayBoard]:
    floors = [
        ParkingFloor.build(1, {SpotType.MOTORCYCLE: 2, SpotType.COMPACT: 3, SpotType.LARGE: 1, SpotType.ELECTRIC: 1}),
        ParkingFloor.build(2, {SpotType.COMPACT: 4, SpotType.LARGE: 2}),
    ]
    board = DisplayBoard()
    for floor in floors:
        floor.subscribe(board)
    lot = ParkingLot("Downtown", floors, clock=clock, ids=SequentialIdGenerator("T"))
    return lot, board


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    lot, board = build_lot(clock)
    entry = EntryGate("entry-1", lot)
    exit_gate = ExitGate("exit-1", lot, pricing=HourlyPricing(), clock=clock, ids=SequentialIdGenerator("P"))

    tickets = [
        entry.issue_ticket("car", "KA01AB1234"),
        entry.issue_ticket("motorcycle", "KA02MC0001"),
        entry.issue_ticket("truck", "KA03TR9999"),
        entry.issue_ticket("electric_car", "KA04EV4242"),
    ]
    for t in tickets:
        print(f"{t.id}: {t.vehicle_type:>12} {t.plate} -> spot {t.spot_id}")
    print("--- display board ---")
    print(board.render())

    clock.advance(2 * 3600 + 35 * 60)  # 2 h 35 min later
    payment = exit_gate.process(tickets[0].id, PaymentMethod.CARD)
    print(f"--- {tickets[0].plate} leaves after 2h35m: charged {payment.amount} ({payment.method}) ---")
    try:
        exit_gate.process(tickets[0].id, PaymentMethod.CARD)
    except TicketStateError as exc:
        print(f"second exit rejected: {exc}")

    lost = exit_gate.process_lost_ticket("KA02MC0001", PaymentMethod.CASH)
    print(f"lost ticket for KA02MC0001: charged {lost.amount}")
    print("--- display board ---")
    print(board.render())
    print(f"free spots in {lot.name}: {lot.free_spots()}")


if __name__ == "__main__":
    main()
