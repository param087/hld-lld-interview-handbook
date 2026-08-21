"""One week in two Lisbon branches: search, book by class, upgrade risk, late return."""

from datetime import date

from common import FakeClock, Money, SequentialIdGenerator
from lld.car_rental.models import (
    AddOnType,
    DateRange,
    NoVehicleAvailableError,
    PaymentMethod,
    VehicleFactory,
    VehicleType,
)
from lld.car_rental.services import Branch, RentalSystem

START_EPOCH = 1_772_355_600.0  # 2026-03-01T09:00Z, so "today" is deterministic


def build_system(clock: FakeClock) -> RentalSystem:
    downtown = Branch(
        "LIS-DOWNTOWN",
        "Lisbon",
        [
            VehicleFactory.create(VehicleType.SUV, "12-SU-01", "LIS-DOWNTOWN", odometer_km=41_200),
            VehicleFactory.create(VehicleType.SUV, "12-SU-02", "LIS-DOWNTOWN", odometer_km=8_050),
            VehicleFactory.create(VehicleType.ECONOMY, "10-EC-01", "LIS-DOWNTOWN"),
        ],
    )
    airport = Branch("LIS-AIRPORT", "Lisbon", [VehicleFactory.create(VehicleType.VAN, "13-VA-01", "LIS-AIRPORT")])
    return RentalSystem([downtown, airport], clock=clock, ids=SequentialIdGenerator("R"))


def main() -> None:
    clock = FakeClock(start=START_EPOCH)
    system = build_system(clock)
    downtown = system.branch("LIS-DOWNTOWN")
    week = DateRange(date(2026, 3, 5), date(2026, 3, 12))

    print(f"today is {system.today()}; searching suv in Lisbon for {week}")
    for branch_id, free in system.search("Lisbon", VehicleType.SUV, week):
        print(f"  {branch_id}: {free} suv free")
    system.schedule_maintenance("LIS-DOWNTOWN", "12-SU-02", DateRange(date(2026, 3, 9), date(2026, 3, 11)), "service")
    system.schedule_maintenance("LIS-DOWNTOWN", "12-SU-01", DateRange(date(2026, 3, 13), date(2026, 3, 15)), "service")
    print(f"workshop booked for both suvs; availability for the week is now {downtown.available(VehicleType.SUV, week)}")

    suv_booking = system.reserve(
        "C1", VehicleType.SUV, "LIS-DOWNTOWN", week, add_ons=(AddOnType.GPS, AddOnType.INSURANCE)
    )
    quote = system.quote(VehicleType.SUV, week, suv_booking.add_ons)
    print(f"{suv_booking.id} reserved: suv {week} with {', '.join(suv_booking.add_ons)} -> quote {quote.total}")
    try:
        system.reserve("C2", VehicleType.SUV, "LIS-DOWNTOWN", week)
    except NoVehicleAvailableError as exc:
        print(f"second suv request rejected: {exc}")
    one_way = system.reserve(
        "C3", VehicleType.ECONOMY, "LIS-DOWNTOWN", DateRange(date(2026, 3, 5), date(2026, 3, 8)), "LIS-AIRPORT"
    )
    print(f"{one_way.id} reserved: economy {one_way.period}, one-way to {one_way.dropoff_branch}")

    clock.advance(4 * 86_400)  # 2026-03-05, pickup day for both
    small = system.pick_up(one_way.id)
    suv = system.pick_up(suv_booking.id)
    print(f"{one_way.id} picked up {small.plate}; {suv_booking.id} picked up {suv.plate} at {suv.odometer_km} km, fuel {suv.fuel_eighths}/8")

    invoice, _ = system.return_vehicle(one_way.id, date(2026, 3, 8), odometer_km=780, fuel_eighths=6)
    print(f"{one_way.id} dropped at LIS-AIRPORT: {invoice.id} totals {invoice.total} (one-way fee included)")

    invoice, displaced = system.return_vehicle(
        suv_booking.id, date(2026, 3, 14), odometer_km=43_900, fuel_eighths=5, damage_fee=Money.of("150.00")
    )
    print(f"{suv_booking.id} back on 2026-03-14, two days late and damaged; it displaced {displaced}")
    for line in invoice.lines:
        print(f"    {line.label}: {line.amount}")
    payment = system.pay(invoice, PaymentMethod.CARD)
    print(f"{payment.id}: {payment.amount} by {payment.method}; 12-SU-01 goes straight to the workshop")


if __name__ == "__main__":
    main()
