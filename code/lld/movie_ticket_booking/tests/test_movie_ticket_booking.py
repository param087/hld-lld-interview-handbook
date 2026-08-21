"""Seat holds, the payment race, idempotency and all-or-nothing multi-seat locking."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from common import ConflictError, FakeClock, Money, SequentialIdGenerator, ValidationError
from lld.movie_ticket_booking.airline import Cabin, FlightLeg, leg_as_show
from lld.movie_ticket_booking.catalog import Catalog
from lld.movie_ticket_booking.models import (
    BookingStateError,
    BookingStatus,
    Cinema,
    City,
    HoldExpiredError,
    Movie,
    PaymentDeclinedError,
    PaymentMethod,
    Screen,
    Seat,
    SeatStatus,
    SeatType,
    SeatUnavailableError,
    Show,
    ShowNotFoundError,
)
from lld.movie_ticket_booking.ports import AlwaysApprovesGateway, NotificationService
from lld.movie_ticket_booking.services import BookingService, HoldExpirySweeper, SeatLockService
from lld.movie_ticket_booking.strategies import (
    NoRefundPolicy,
    SeatTypePricing,
    TieredRefundPolicy,
    WeekendSurgePricing,
)

START = 1_700_000_000.0
SHOW_TIME = START + 6 * 3600


class DecliningGateway:
    def __init__(self) -> None:
        self.refunds: list[tuple[str, Money]] = []

    def charge(self, payment_id: str, amount: Money, method: PaymentMethod) -> bool:
        return False

    def refund(self, payment_id: str, amount: Money) -> None:
        self.refunds.append((payment_id, amount))


def build(clock: FakeClock, rows: int = 2, per_row: int = 4, **kwargs: object) -> tuple[Catalog, Show, BookingService, SeatLockService]:
    letters = "AB CDEFGH".replace(" ", "")
    seats = tuple(
        Seat(f"{letters[r]}{i}", letters[r], SeatType.PREMIUM if r else SeatType.REGULAR)
        for r in range(rows)
        for i in range(1, per_row + 1)
    )
    screen = Screen("SC-1", "CN-1", "Audi 1", seats)
    catalog = Catalog()
    catalog.add_city(City("blr", "Bengaluru"))
    catalog.add_cinema(Cinema("CN-1", "blr", "PVR Forum", (screen,)))
    catalog.add_movie(Movie("MV-1", "Interstellar", "English", 169))
    show = Show.for_screen("SH-1", "MV-1", screen, SHOW_TIME, Money.of("100.00"))
    catalog.add_show(show)
    locks = SeatLockService(clock=clock, hold_ttl_seconds=600)
    service = BookingService(
        catalog,
        locks,
        clock=clock,
        ids=SequentialIdGenerator("BK"),
        payment_ids=SequentialIdGenerator("PAY"),
        **kwargs,
    )
    return catalog, show, service, locks


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


def test_hold_then_pay_confirms_and_notifies(clock: FakeClock) -> None:
    _, show, service, _ = build(clock)
    notifier = NotificationService()
    service.subscribe(notifier)
    booking = service.create_booking("SH-1", ["A1", "A2"], user_id="u-1")
    assert booking.status is BookingStatus.PENDING
    assert booking.amount == Money.of("200.00")  # 2 regular seats x 100.00
    assert booking.seconds_left(clock.now()) == 600
    assert show.seat("A1").status is SeatStatus.HELD

    service.pay(booking.id, PaymentMethod.CARD, idempotency_key="k1")
    assert booking.status is BookingStatus.CONFIRMED
    assert show.seat("A1").status is SeatStatus.BOOKED
    assert show.seat("A1").booking_id == booking.id
    assert notifier.outbox() == ["booking_confirmed: BK-1 seats A1,A2 (200.00 USD)"]


def test_overlapping_request_is_all_or_nothing(clock: FakeClock) -> None:
    _, show, service, _ = build(clock)
    service.create_booking("SH-1", ["A2", "A3"], user_id="u-1")
    with pytest.raises(SeatUnavailableError, match="A3"):
        service.create_booking("SH-1", ["A3", "A4"], user_id="u-2")
    # A4 must not have been half-held by the failed request.
    assert show.seat("A4").status is SeatStatus.AVAILABLE
    assert show.available(clock.now()) == ["A1", "A4", "B1", "B2", "B3", "B4"]


@pytest.mark.parametrize(
    ("seats", "error"),
    [
        ((), ValidationError),
        (("A1", "A1"), ValidationError),
        (tuple(f"A{i}" for i in range(1, 12)), ValidationError),
        (("Z9",), ShowNotFoundError),
    ],
)
def test_invalid_seat_requests_are_rejected_before_any_lock(
    clock: FakeClock, seats: tuple[str, ...], error: type[Exception]
) -> None:
    _, show, service, _ = build(clock)
    with pytest.raises(error):
        service.create_booking("SH-1", seats, user_id="u-1")
    assert all(s.status is SeatStatus.AVAILABLE for s in show.all_seats())


# --8<-- [start:sweeper_race]
def test_sweeper_beats_a_late_payment_and_the_charge_is_refunded(clock: FakeClock) -> None:
    gateway = AlwaysApprovesGateway()
    _, show, service, locks = build(clock, gateway=gateway)
    booking = service.create_booking("SH-1", ["A1"], user_id="u-1")

    clock.advance(601)  # the 10-minute hold has lapsed
    assert locks.sweep(show) == [booking.id]  # the sweeper got there first
    assert show.seat("A1").status is SeatStatus.AVAILABLE

    with pytest.raises(HoldExpiredError):
        service.pay(booking.id, PaymentMethod.UPI, idempotency_key="k-late")
    assert booking.status is BookingStatus.EXPIRED
    assert gateway.refunds == [("PAY-1", Money.of("100.00"))]  # money never sticks


# --8<-- [end:sweeper_race]


def test_payment_before_the_sweep_wins_and_the_sweep_is_a_no_op(clock: FakeClock) -> None:
    _, show, service, locks = build(clock)
    booking = service.create_booking("SH-1", ["A1"], user_id="u-1")
    clock.advance(601)
    service.pay(booking.id, PaymentMethod.CARD, idempotency_key="k1")  # lazy expiry never fires
    assert booking.status is BookingStatus.CONFIRMED
    assert locks.sweep(show) == []
    assert show.seat("A1").status is SeatStatus.BOOKED


def test_expiry_sweeper_marks_the_booking_expired_and_frees_the_seats(clock: FakeClock) -> None:
    _, show, service, _ = build(clock)
    booking = service.create_booking("SH-1", ["B1", "B2"], user_id="u-1")
    sweeper = HoldExpirySweeper(service, interval_seconds=30.0)
    assert sweeper.run_once() == []  # nothing has expired yet
    clock.advance(600)
    assert sweeper.run_once() == [booking.id]
    assert booking.status is BookingStatus.EXPIRED
    assert show.seat("B1").status is SeatStatus.AVAILABLE
    with pytest.raises(BookingStateError):
        service.pay(booking.id, PaymentMethod.CARD, idempotency_key="k2")


def test_replaying_the_idempotency_key_never_charges_twice(clock: FakeClock) -> None:
    charges: list[str] = []

    class CountingGateway(AlwaysApprovesGateway):
        def charge(self, payment_id: str, amount: Money, method: PaymentMethod) -> bool:
            charges.append(payment_id)
            return True

    _, _, service, _ = build(clock, gateway=CountingGateway())
    booking = service.create_booking("SH-1", ["A1"], user_id="u-1")
    first = service.pay(booking.id, PaymentMethod.CARD, idempotency_key="same-key")
    second = service.pay(booking.id, PaymentMethod.CARD, idempotency_key="same-key")
    assert first is second and first.status is BookingStatus.CONFIRMED
    assert charges == ["PAY-1"]


def test_declined_payment_keeps_the_hold_so_the_user_can_retry(clock: FakeClock) -> None:
    gateway = DecliningGateway()
    _, show, service, _ = build(clock, gateway=gateway)
    booking = service.create_booking("SH-1", ["A1"], user_id="u-1")
    with pytest.raises(PaymentDeclinedError):
        service.pay(booking.id, PaymentMethod.CARD, idempotency_key="k1")
    assert booking.status is BookingStatus.PENDING
    assert show.seat("A1").status is SeatStatus.HELD
    with pytest.raises(PaymentDeclinedError, match="use a new key"):
        service.pay(booking.id, PaymentMethod.CARD, idempotency_key="k1")


def test_cancellation_refunds_by_policy_and_puts_seats_back(clock: FakeClock) -> None:
    gateway = AlwaysApprovesGateway()
    _, show, service, _ = build(clock, gateway=gateway, refunds=TieredRefundPolicy(4 * 3600))
    booking = service.create_booking("SH-1", ["A1", "A2"], user_id="u-1")
    service.pay(booking.id, PaymentMethod.WALLET, idempotency_key="k1")
    clock.advance(3 * 3600)  # 3 h before the show, inside the 4 h cut-off
    assert service.cancel(booking.id) == Money.of("100.00")  # half of 200.00
    assert booking.status is BookingStatus.CANCELLED
    assert show.seat("A1").status is SeatStatus.AVAILABLE
    with pytest.raises(BookingStateError):
        service.cancel(booking.id)


@pytest.mark.parametrize(
    ("policy", "advance", "expected"),
    [
        (TieredRefundPolicy(4 * 3600), 0, "200.00"),
        (TieredRefundPolicy(4 * 3600), 3 * 3600, "100.00"),
        (TieredRefundPolicy(4 * 3600), 6 * 3600, "0.00"),
        (NoRefundPolicy(), 0, "0.00"),
    ],
)
def test_refund_policies(clock: FakeClock, policy: object, advance: int, expected: str) -> None:
    _, _, service, _ = build(clock, refunds=policy)
    booking = service.create_booking("SH-1", ["A1", "A2"], user_id="u-1")
    service.pay(booking.id, PaymentMethod.CARD, idempotency_key="k1")
    clock.advance(advance)
    assert service.cancel(booking.id) == Money.of(expected)


def test_pricing_scales_by_seat_type_and_weekend_surge(clock: FakeClock) -> None:
    _, show, service, _ = build(clock)
    assert service.quote("SH-1", ["A1", "B1"]) == Money.of("250.00")  # 100 regular + 150 premium
    surge = WeekendSurgePricing(SeatTypePricing(), Decimal("1.2"))
    one_screen = Screen("SC-1", "CN-1", "Audi 1", (Seat("A1", "A"),))
    saturday = Show.for_screen("SH-2", "MV-1", one_screen, 1_700_310_000.0, Money.of("100.00"))
    assert surge.price(saturday, saturday.seat("A1")) == Money.of("120.00")  # Sat 18 Nov 2023
    assert surge.price(show, show.seat("A1")) == Money.of("100.00")  # Tue 14 Nov 2023


# --8<-- [start:concurrency]
def test_forty_users_race_for_eight_seats_and_nobody_gets_a_shared_seat(clock: FakeClock) -> None:
    _, show, service, _ = build(clock, rows=2, per_row=4)
    pairs = [("A1", "A2"), ("A2", "A3"), ("A3", "A4"), ("B1", "B2"), ("B2", "B3"), ("B3", "B4")]

    def attempt(i: int) -> tuple[str, ...] | None:
        try:
            return service.create_booking("SH-1", pairs[i % len(pairs)], f"u-{i}").seat_numbers
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(40)))

    won = [seats for seats in results if seats is not None]
    claimed = [number for seats in won for number in seats]
    assert len(claimed) == len(set(claimed))  # no seat sold to two bookings
    held = [s.number for s in show.all_seats() if s.status is SeatStatus.HELD]
    assert sorted(claimed) == sorted(held)  # every winner's seats are held, nothing else is
    assert 2 <= len(won) <= 4  # overlapping pairs: at most four of the six can coexist


# --8<-- [end:concurrency]


def test_airline_variant_reuses_the_same_services(clock: FakeClock) -> None:
    leg = FlightLeg(
        "LEG-1", "AI2841", "BLR", "DEL", SHOW_TIME, Money.of("4500.00"), (Cabin("business", 1, 2, "AC"),)
    )
    city, cinema, flight = leg_as_show(leg)
    catalog = Catalog()
    catalog.add_city(city)
    catalog.add_cinema(cinema)
    catalog.add_show(flight)
    service = BookingService(
        catalog, SeatLockService(clock=clock), clock=clock, ids=SequentialIdGenerator("BK")
    )
    booking = service.create_booking("LEG-1", ["1A", "1C"], user_id="flyer")
    assert booking.amount == Money.of("22500.00")  # 2 business seats x 4500 x 2.5
    service.pay(booking.id, PaymentMethod.CARD, idempotency_key="k1")
    assert booking.status is BookingStatus.CONFIRMED
    assert flight.seat("1A").status is SeatStatus.BOOKED
