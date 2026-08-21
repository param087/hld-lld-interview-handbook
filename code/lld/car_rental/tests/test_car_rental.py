from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from common import FakeClock, Money, SequentialIdGenerator, ValidationError
from lld.car_rental.models import (
    AddOnType,
    DateRange,
    NoVehicleAvailableError,
    OverlappingReservationError,
    PaymentMethod,
    ReservationStateError,
    ReservationStatus,
    VehicleFactory,
    VehicleStatus,
    VehicleType,
)
from lld.car_rental.services import Branch, RentalSystem
from lld.car_rental.strategies import AddOnFactory, DailyRate, WeeklyRate

START_EPOCH = 1_772_355_600.0  # 2026-03-01T09:00Z
WEEK = DateRange(date(2026, 3, 5), date(2026, 3, 12))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START_EPOCH)


def make_branch(branch_id: str, city: str, fleet: dict[VehicleType, int]) -> Branch:
    vehicles = [
        VehicleFactory.create(vehicle_type, f"{branch_id}-{vehicle_type[:2].upper()}{i}", branch_id)
        for vehicle_type, count in fleet.items()
        for i in range(1, count + 1)
    ]
    return Branch(branch_id, city, vehicles)


def make_system(clock: FakeClock, *branches: Branch) -> RentalSystem:
    return RentalSystem(branches, clock=clock, ids=SequentialIdGenerator("R"))


def test_reserve_pick_up_and_return_bills_base_add_ons_and_extras(clock: FakeClock) -> None:
    system = make_system(clock, make_branch("LIS", "Lisbon", {VehicleType.SUV: 1}))
    booking = system.reserve("c1", VehicleType.SUV, "LIS", WEEK, add_ons=(AddOnType.GPS,))
    assert system.search("Lisbon", VehicleType.SUV, WEEK) == []  # the only suv is now held

    clock.advance(4 * 86_400)
    car = system.pick_up(booking.id)
    assert car.status is VehicleStatus.RENTED and booking.status is ReservationStatus.PICKED_UP

    invoice, displaced = system.return_vehicle(booking.id, date(2026, 3, 12), odometer_km=900, fuel_eighths=8)
    # 7 days billed as one week (6 x 65.00 = 390.00) + GPS 7 x 4.00; nothing else applies.
    assert invoice.total == Money.of("418.00") and displaced == []
    assert booking.status is ReservationStatus.RETURNED and car.status is VehicleStatus.AVAILABLE
    assert system.pay(invoice, PaymentMethod.CARD).amount == invoice.total


@pytest.mark.parametrize(
    ("other", "expected"),
    [
        (DateRange(date(2026, 3, 12), date(2026, 3, 14)), False),  # starts the day this one ends
        (DateRange(date(2026, 3, 1), date(2026, 3, 5)), False),  # ends the day this one starts
        (DateRange(date(2026, 3, 11), date(2026, 3, 13)), True),  # one shared day
        (DateRange(date(2026, 3, 6), date(2026, 3, 8)), True),  # fully inside
        (DateRange(date(2026, 3, 1), date(2026, 4, 1)), True),  # fully around
    ],
)
def test_date_ranges_are_half_open(other: DateRange, expected: bool) -> None:
    assert WEEK.overlaps(other) is expected and other.overlaps(WEEK) is expected


def test_back_to_back_rentals_reuse_the_same_car(clock: FakeClock) -> None:
    system = make_system(clock, make_branch("LIS", "Lisbon", {VehicleType.SEDAN: 1}))
    first = system.reserve("c1", VehicleType.SEDAN, "LIS", WEEK)
    second = system.reserve("c2", VehicleType.SEDAN, "LIS", DateRange(date(2026, 3, 12), date(2026, 3, 15)))
    clock.advance(4 * 86_400)
    plate = system.pick_up(first.id).plate
    system.return_vehicle(first.id, date(2026, 3, 12), odometer_km=100, fuel_eighths=8)
    clock.advance(7 * 86_400)
    assert system.pick_up(second.id).plate == plate


def test_reserve_rejects_when_every_car_of_the_class_is_committed(clock: FakeClock) -> None:
    system = make_system(clock, make_branch("LIS", "Lisbon", {VehicleType.VAN: 2}))
    system.reserve("c1", VehicleType.VAN, "LIS", WEEK)
    system.reserve("c2", VehicleType.VAN, "LIS", WEEK)
    with pytest.raises(NoVehicleAvailableError):
        system.reserve("c3", VehicleType.VAN, "LIS", WEEK)
    # A non-overlapping window is still bookable: holds are per date range, not per class.
    assert system.reserve("c3", VehicleType.VAN, "LIS", DateRange(date(2026, 3, 12), date(2026, 3, 14)))


def test_booking_a_past_date_is_rejected(clock: FakeClock) -> None:
    system = make_system(clock, make_branch("LIS", "Lisbon", {VehicleType.SUV: 1}))
    with pytest.raises(ValidationError):
        system.reserve("c1", VehicleType.SUV, "LIS", DateRange(date(2026, 2, 20), date(2026, 2, 25)))
    with pytest.raises(ValidationError):
        DateRange(date(2026, 3, 5), date(2026, 3, 5))  # zero-day rentals do not exist


# --8<-- [start:concurrency]
def test_concurrent_reservations_never_oversell_a_branch(clock: FakeClock) -> None:
    system = make_system(clock, make_branch("LIS", "Lisbon", {VehicleType.SUV: 3}))

    def book(i: int) -> str | None:
        try:
            return system.reserve(f"c{i}", VehicleType.SUV, "LIS", WEEK).id
        except NoVehicleAvailableError:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(book, range(24)))

    winners = [r for r in results if r is not None]
    assert len(winners) == 3 and len(set(winners)) == 3  # three cars, three winners, no oversell
    assert system.branch("LIS").available(VehicleType.SUV, WEEK) == 0


# --8<-- [end:concurrency]


# --8<-- [start:upgrade]
def test_pickup_walks_the_upgrade_ladder_when_the_booked_class_is_out(clock: FakeClock) -> None:
    branch = make_branch("LIS", "Lisbon", {VehicleType.ECONOMY: 1, VehicleType.SEDAN: 1})
    system = make_system(clock, branch)
    booking = system.reserve("c1", VehicleType.ECONOMY, "LIS", WEEK)
    # The economy car breaks down after the booking is taken; the desk still owes a car.
    system.schedule_maintenance("LIS", "LIS-EC1", DateRange(date(2026, 3, 4), date(2026, 3, 20)), "gearbox")

    clock.advance(4 * 86_400)
    handed_over = system.pick_up(booking.id)
    assert handed_over.vehicle_type is VehicleType.SEDAN  # upgraded at the desk...

    invoice, _ = system.return_vehicle(booking.id, date(2026, 3, 12), odometer_km=100, fuel_eighths=8)
    assert invoice.total == Money.of("210.00")  # ...but billed at the economy week rate, 6 x 35.00


# --8<-- [end:upgrade]


def test_cancel_frees_the_slot_and_blocks_a_second_cancel(clock: FakeClock) -> None:
    system = make_system(clock, make_branch("LIS", "Lisbon", {VehicleType.SUV: 1}))
    booking = system.reserve("c1", VehicleType.SUV, "LIS", WEEK)
    assert system.cancel(booking.id) == Money(0)  # four days ahead: free
    assert booking.status is ReservationStatus.CANCELLED
    assert system.branch("LIS").available(VehicleType.SUV, WEEK) == 1
    with pytest.raises(ReservationStateError):
        system.cancel(booking.id)
    late = system.reserve("c2", VehicleType.SUV, "LIS", DateRange(date(2026, 3, 1), date(2026, 3, 3)))
    assert system.cancel(late.id) == Money.of("65.00")  # same-day cancellation costs one day


def test_maintenance_hides_a_car_and_rejects_an_overlapping_window(clock: FakeClock) -> None:
    branch = make_branch("LIS", "Lisbon", {VehicleType.SUV: 1})
    system = make_system(clock, branch)
    system.schedule_maintenance("LIS", "LIS-SU1", DateRange(date(2026, 3, 6), date(2026, 3, 8)), "service")
    assert branch.available(VehicleType.SUV, WEEK) == 0
    with pytest.raises(OverlappingReservationError):
        system.schedule_maintenance("LIS", "LIS-SU1", DateRange(date(2026, 3, 7), date(2026, 3, 9)), "tyres")
    assert branch.available(VehicleType.SUV, DateRange(date(2026, 3, 8), date(2026, 3, 10))) == 1


def test_one_way_return_moves_the_car_to_the_drop_off_branch(clock: FakeClock) -> None:
    downtown = make_branch("LIS-A", "Lisbon", {VehicleType.ECONOMY: 1})
    airport = make_branch("LIS-B", "Lisbon", {})
    system = make_system(clock, downtown, airport)
    booking = system.reserve("c1", VehicleType.ECONOMY, "LIS-A", WEEK, dropoff_branch="LIS-B")
    clock.advance(4 * 86_400)
    car = system.pick_up(booking.id)
    invoice, _ = system.return_vehicle(booking.id, date(2026, 3, 12), odometer_km=1_600, fuel_eighths=8)
    assert car.branch_id == "LIS-B"
    assert downtown.fleet_size(VehicleType.ECONOMY) == 0 and airport.fleet_size(VehicleType.ECONOMY) == 1
    # 6 x 35.00 week rate + 75.00 one-way + (1600 - 7 x 200) km x 0.25
    assert invoice.total == Money.of("335.00")


def test_late_return_charges_extra_days_and_displaces_the_service_slot(clock: FakeClock) -> None:
    system = make_system(clock, make_branch("LIS", "Lisbon", {VehicleType.SUV: 1}))
    booking = system.reserve("c1", VehicleType.SUV, "LIS", WEEK)
    system.schedule_maintenance("LIS", "LIS-SU1", DateRange(date(2026, 3, 13), date(2026, 3, 15)), "service")
    clock.advance(4 * 86_400)
    system.pick_up(booking.id)
    invoice, displaced = system.return_vehicle(
        booking.id, date(2026, 3, 14), odometer_km=100, fuel_eighths=8, damage_fee=Money.of("150.00")
    )
    assert displaced == ["maint:M-1"]  # the late car ate into its own workshop slot
    # 390.00 week + 2 late days at 1.5 x 65.00 + 150.00 damage
    assert invoice.total == Money.of("735.00")
    assert system.branch("LIS").available(VehicleType.SUV, DateRange(date(2026, 3, 14), date(2026, 3, 16))) == 0


@pytest.mark.parametrize(
    ("add_ons", "days", "expected"),
    [
        ((), 3, "195.00"),  # 3 x 65.00
        ((), 7, "390.00"),  # a week costs six days
        ((), 9, "520.00"),  # one week + 2 days
        ((AddOnType.GPS,), 7, "418.00"),  # + 7 x 4.00
        ((AddOnType.GPS, AddOnType.INSURANCE), 7, "501.60"),  # + 20% of 418.00
    ],
)
def test_rate_plan_and_add_on_decorators(add_ons: tuple[AddOnType, ...], days: int, expected: str) -> None:
    plan = AddOnFactory.decorate(WeeklyRate(DailyRate()), add_ons)
    lines = plan.price(VehicleType.SUV, days)
    assert sum(line.amount.cents for line in lines) == Money.of(expected).cents
