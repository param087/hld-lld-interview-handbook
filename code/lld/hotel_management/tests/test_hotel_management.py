"""Overlapping date ranges, the per-room-type lock, check-in assignment and invoices."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest

from common import FakeClock, Money, SequentialIdGenerator, ValidationError
from lld.hotel_management.hotel import Hotel, HotelBuilder
from lld.hotel_management.models import (
    DateRange,
    NoAvailabilityError,
    NoRoomReadyError,
    PaymentDeclinedError,
    PaymentMethod,
    Reservation,
    ReservationStateError,
    ReservationStatus,
    Room,
    RoomRequest,
    RoomStatus,
    RoomType,
    Staff,
    StaffRole,
    TaskKind,
)
from lld.hotel_management.ports import HousekeepingService, NotificationService
from lld.hotel_management.services import AvailabilityService, FrontDeskService
from lld.hotel_management.strategies import (
    FlatRatePricing,
    FreeUntilDaysBefore,
    NonRefundablePolicy,
    SeasonalPricing,
)

TODAY_EPOCH = 1_773_219_600.0  # 2026-03-11 09:00 UTC
STAY = DateRange(date(2026, 3, 11), date(2026, 3, 14))  # three nights


class DecliningGateway:
    def charge(self, payment_id: str, amount: Money, method: PaymentMethod) -> bool:
        return False

    def refund(self, payment_id: str, amount: Money) -> None:
        raise AssertionError("a declined charge is never refunded")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=TODAY_EPOCH)


def make_hotel(doubles: int = 2, suites: int = 1) -> Hotel:
    builder = HotelBuilder().named("Seaside Grand").with_rooms(RoomType.DOUBLE, doubles, floor=1)
    if suites:
        builder = builder.with_rooms(RoomType.SUITE, suites, floor=3)
    return builder.build()


def make_desk(clock: FakeClock, hotel: Hotel, **kwargs: object) -> tuple[FrontDeskService, AvailabilityService]:
    availability = AvailabilityService(hotel, overbooking=kwargs.pop("overbooking", None))
    desk = FrontDeskService(
        hotel,
        availability,
        clock=clock,
        ids=SequentialIdGenerator("RSV"),
        payment_ids=SequentialIdGenerator("PAY"),
        **kwargs,
    )
    return desk, availability


def test_reserve_pay_check_in_check_out_produces_an_invoice(clock: FakeClock) -> None:
    hotel = make_hotel(doubles=2)
    desk, availability = make_desk(clock, hotel, pricing=FlatRatePricing())
    housekeeping = HousekeepingService(clock=clock, ids=SequentialIdGenerator("HK"))
    notifier = NotificationService()
    desk.subscribe(housekeeping)
    desk.subscribe(notifier)

    reservation = desk.reserve("g-1", [RoomRequest(RoomType.DOUBLE, 1)], STAY)
    assert reservation.amount == Money.of("360.00")  # 3 nights x 120.00
    assert availability.available(RoomType.DOUBLE, STAY) == 1

    desk.pay(reservation.id, PaymentMethod.CARD, idempotency_key="k1")
    assert reservation.status is ReservationStatus.CONFIRMED
    assert desk.check_in(reservation.id) == ("101",)
    assert hotel.room("101").status is RoomStatus.OCCUPIED

    desk.add_charge(reservation.id, "minibar", Money.of("45.50"))
    invoice = desk.check_out(reservation.id)
    assert invoice.subtotal() == Money.of("405.50")
    assert invoice.tax == Money.of("48.66")  # 12% of 405.50
    assert invoice.total == Money.of("454.16")
    assert hotel.room("101").status is RoomStatus.CLEANING
    assert [t.kind for t in housekeeping.open_tasks()] == [TaskKind.TURNDOWN]
    assert availability.available(RoomType.DOUBLE, STAY) == 2  # nights back on sale
    assert "checked_out" in notifier.outbox()[-1]


# --8<-- [start:overlap]
@pytest.mark.parametrize(
    ("start", "end", "expected_free"),
    [
        (date(2026, 3, 8), date(2026, 3, 11), 2),  # ends the morning we arrive: no overlap
        (date(2026, 3, 14), date(2026, 3, 16), 2),  # starts the morning we leave: no overlap
        (date(2026, 3, 10), date(2026, 3, 12), 1),  # straddles the first night
        (date(2026, 3, 13), date(2026, 3, 15), 1),  # straddles the last night
        (date(2026, 3, 9), date(2026, 3, 20), 1),  # swallows the whole stay
    ],
)
def test_half_open_ranges_touch_without_overlapping(
    clock: FakeClock, start: date, end: date, expected_free: int
) -> None:
    hotel = make_hotel(doubles=2)
    desk, availability = make_desk(clock, hotel)
    desk.reserve("g-1", [RoomRequest(RoomType.DOUBLE, 1)], STAY)
    assert availability.available(RoomType.DOUBLE, DateRange(start, end)) == expected_free


# --8<-- [end:overlap]


def test_selling_past_capacity_is_refused_and_overbooking_allows_one_more(clock: FakeClock) -> None:
    hotel = make_hotel(doubles=2)
    desk, _ = make_desk(clock, hotel)
    desk.reserve("g-1", [RoomRequest(RoomType.DOUBLE, 2)], STAY)
    with pytest.raises(NoAvailabilityError, match="1 x double"):
        desk.reserve("g-2", [RoomRequest(RoomType.DOUBLE, 1)], STAY)

    generous_hotel = make_hotel(doubles=2)
    generous, _ = make_desk(clock, generous_hotel, overbooking={RoomType.DOUBLE: 1})
    generous.reserve("g-1", [RoomRequest(RoomType.DOUBLE, 2)], STAY)
    assert generous.reserve("g-2", [RoomRequest(RoomType.DOUBLE, 1)], STAY).room_count == 1


def test_state_machine_rejects_out_of_order_transitions(clock: FakeClock) -> None:
    hotel = make_hotel(doubles=1)
    desk, _ = make_desk(clock, hotel)
    reservation = desk.reserve("g-1", [RoomRequest(RoomType.DOUBLE, 1)], STAY)
    with pytest.raises(ReservationStateError):
        desk.check_in(reservation.id)  # still PENDING
    desk.pay(reservation.id, PaymentMethod.CARD, idempotency_key="k1")
    with pytest.raises(ReservationStateError):
        desk.check_out(reservation.id)  # CONFIRMED, not CHECKED_IN
    desk.check_in(reservation.id)
    with pytest.raises(ReservationStateError):
        desk.pay(reservation.id, PaymentMethod.CARD, idempotency_key="k2")
    desk.check_out(reservation.id)
    with pytest.raises(ReservationStateError):
        desk.cancel(reservation.id)


def test_check_in_needs_a_clean_room_and_rolls_back_a_partial_assignment(clock: FakeClock) -> None:
    hotel = make_hotel(doubles=2)
    desk, _ = make_desk(clock, hotel)
    hotel.room("102").status = RoomStatus.CLEANING  # housekeeping is running late
    reservation = desk.reserve("g-1", [RoomRequest(RoomType.DOUBLE, 2)], STAY)
    desk.pay(reservation.id, PaymentMethod.CARD, idempotency_key="k1")
    with pytest.raises(NoRoomReadyError):
        desk.check_in(reservation.id)
    assert hotel.room("101").status is RoomStatus.AVAILABLE  # nothing half-assigned
    assert reservation.status is ReservationStatus.CONFIRMED
    hotel.room("102").mark_clean()
    assert desk.check_in(reservation.id) == ("101", "102")


def test_declined_card_keeps_the_reservation_pending_and_retryable(clock: FakeClock) -> None:
    hotel = make_hotel(doubles=1)
    desk, availability = make_desk(clock, hotel, gateway=DecliningGateway())
    reservation = desk.reserve("g-1", [RoomRequest(RoomType.DOUBLE, 1)], STAY)
    with pytest.raises(PaymentDeclinedError):
        desk.pay(reservation.id, PaymentMethod.CARD, idempotency_key="k1")
    assert reservation.status is ReservationStatus.PENDING
    assert availability.available(RoomType.DOUBLE, STAY) == 0  # the hold survives


def test_no_show_sweep_frees_the_nights(clock: FakeClock) -> None:
    hotel = make_hotel(doubles=1)
    desk, availability = make_desk(clock, hotel)
    past = DateRange(date(2026, 3, 9), date(2026, 3, 12))
    reservation = desk.reserve("g-1", [RoomRequest(RoomType.DOUBLE, 1)], past)
    desk.pay(reservation.id, PaymentMethod.CASH, idempotency_key="k1")
    assert desk.sweep_no_shows() == [reservation.id]
    assert reservation.status is ReservationStatus.NO_SHOW
    assert availability.available(RoomType.DOUBLE, past) == 1


@pytest.mark.parametrize(
    ("policy", "today", "expected"),
    [
        (FreeUntilDaysBefore(2), date(2026, 3, 8), "360.00"),  # three days out: free
        (FreeUntilDaysBefore(2), date(2026, 3, 10), "240.00"),  # one day out: keep a night
        (FreeUntilDaysBefore(2), date(2026, 3, 11), "0.00"),  # arrival day
        (NonRefundablePolicy(), date(2026, 3, 1), "0.00"),
    ],
)
def test_cancellation_policies(policy: object, today: date, expected: str) -> None:
    reservation = Reservation(
        id="RSV-1",
        guest_id="g-1",
        stay=STAY,
        rooms=(RoomRequest(RoomType.DOUBLE, 1),),
        amount=Money.of("360.00"),
    )
    assert policy.refund(reservation, today) == Money.of(expected)


def test_seasonal_pricing_and_builder_validation() -> None:
    pricing = SeasonalPricing()
    assert pricing.price_night(RoomType.DOUBLE, date(2026, 3, 11)) == Money.of("120.00")
    assert pricing.price_night(RoomType.DOUBLE, date(2026, 7, 11)) == Money.of("180.00")  # x1.5
    with pytest.raises(ValidationError, match="needs a name"):
        HotelBuilder().with_rooms(RoomType.DOUBLE, 1, floor=1).build()
    with pytest.raises(ValidationError, match="at least one room"):
        HotelBuilder().named("Empty").build()
    with pytest.raises(ValidationError):
        HotelBuilder().named("Bad").with_tax_rate(Decimal("-1"))
    with pytest.raises(ValidationError):
        DateRange(date(2026, 3, 11), date(2026, 3, 11))  # zero-night stay


def test_housekeeping_role_check_and_deep_clean_for_long_stays(clock: FakeClock) -> None:
    housekeeping = HousekeepingService(clock=clock, ids=SequentialIdGenerator("HK"))
    long_stay = Reservation(
        id="RSV-9",
        guest_id="g-1",
        stay=DateRange(date(2026, 3, 1), date(2026, 3, 9)),
        rooms=(RoomRequest(RoomType.SUITE, 1),),
        amount=Money.of("100.00"),
    )
    room = Room(number="301", floor=3, type=RoomType.SUITE)
    housekeeping.on_stay_event("checked_out", long_stay, room)
    task = housekeeping.open_tasks()[0]
    assert task.kind is TaskKind.DEEP_CLEAN  # eight nights
    assert task.room_number == "301"
    with pytest.raises(ValidationError):
        housekeeping.assign(task.id, Staff("s-2", "Meera", StaffRole.MANAGER))
    assert housekeeping.assign(task.id, Staff("s-1", "Ravi", StaffRole.HOUSEKEEPER)).assigned_to == "s-1"


# --8<-- [start:concurrency]
def test_thirty_agents_race_for_five_rooms_and_never_oversell(clock: FakeClock) -> None:
    hotel = make_hotel(doubles=5, suites=0)
    desk, availability = make_desk(clock, hotel)
    peak = DateRange(date(2026, 3, 11), date(2026, 3, 14))

    def book(i: int) -> str | None:
        try:
            return desk.reserve(f"g-{i}", [RoomRequest(RoomType.DOUBLE, 1)], peak).id
        except NoAvailabilityError:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(book, range(30)))

    winners = [r for r in results if r is not None]
    assert len(winners) == 5 and len(set(winners)) == 5  # each room sold exactly once
    assert all(free == 0 for free in availability.calendar(RoomType.DOUBLE, peak).values())
    later = DateRange(date(2026, 3, 14), date(2026, 3, 17))
    assert availability.available(RoomType.DOUBLE, later) == 5  # the counter is per night


# --8<-- [end:concurrency]
