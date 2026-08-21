"""One screen, four users: hold, pay, replay, expire, cancel - plus the airline variant."""

from common import FakeClock, Money, SequentialIdGenerator
from lld.movie_ticket_booking.airline import Cabin, FlightLeg, leg_as_show
from lld.movie_ticket_booking.catalog import Catalog
from lld.movie_ticket_booking.models import (
    Cinema,
    City,
    HoldExpiredError,
    Movie,
    PaymentMethod,
    Screen,
    Seat,
    SeatType,
    SeatUnavailableError,
    Show,
)
from lld.movie_ticket_booking.ports import AlwaysApprovesGateway, NotificationService
from lld.movie_ticket_booking.services import BookingService, SeatLockService

START = 1_700_000_000.0
SHOW_TIME = START + 3 * 3600


def build_catalog(clock: FakeClock) -> tuple[Catalog, Show]:
    seats = tuple(
        [Seat(f"A{i}", "A", SeatType.REGULAR) for i in range(1, 5)]
        + [Seat(f"B{i}", "B", SeatType.PREMIUM) for i in range(1, 3)]
        + [Seat("C1", "C", SeatType.RECLINER)]
    )
    screen = Screen(id="SC-1", cinema_id="CN-1", name="Audi 1", seats=seats)
    catalog = Catalog()
    catalog.add_city(City("blr", "Bengaluru"))
    catalog.add_cinema(Cinema("CN-1", "blr", "PVR Forum", (screen,)))
    catalog.add_movie(Movie("MV-1", "Interstellar", "English", 169))
    show = Show.for_screen("SH-1", "MV-1", screen, SHOW_TIME, Money.of("250.00"))
    catalog.add_show(show)
    return catalog, show


def main() -> None:
    clock = FakeClock(start=START)
    catalog, show = build_catalog(clock)
    locks = SeatLockService(clock=clock, hold_ttl_seconds=600)
    gateway = AlwaysApprovesGateway()
    notifier = NotificationService()
    bookings = BookingService(
        catalog,
        locks,
        gateway=gateway,
        clock=clock,
        ids=SequentialIdGenerator("BK"),
        payment_ids=SequentialIdGenerator("PAY"),
    )
    bookings.subscribe(notifier)

    hit = catalog.search("inter", city_id="blr")[0]
    print(f"search 'inter' in Bengaluru -> {hit.id} at PVR Forum, {len(hit.available(clock.now()))} seats free")

    first = bookings.create_booking("SH-1", ["A1", "A2"], user_id="u-asha")
    print(f"{first.id} holds {','.join(first.seat_numbers)} for {first.seconds_left(clock.now()):.0f}s -> {first.status}, {first.amount}")
    try:
        bookings.create_booking("SH-1", ["A2", "A3"], user_id="u-bala")
    except SeatUnavailableError as exc:
        print(f"second user rejected: {exc}")

    bookings.pay(first.id, PaymentMethod.CARD, idempotency_key="pay-alpha")
    print(f"{first.id} paid -> {first.status} via {first.payment_id}")
    bookings.pay(first.id, PaymentMethod.CARD, idempotency_key="pay-alpha")
    print(f"replay of key pay-alpha -> {first.status}, refunds issued so far: {len(gateway.refunds)}")

    late = bookings.create_booking("SH-1", ["B1", "B2"], user_id="u-chitra")
    print(f"{late.id} holds {','.join(late.seat_numbers)} -> {late.status}, {late.amount}")
    clock.advance(11 * 60)
    print(f"11 minutes pass; sweeper reclaims {locks.sweep(show)}")
    try:
        bookings.pay(late.id, PaymentMethod.UPI, idempotency_key="pay-beta")
    except HoldExpiredError as exc:
        print(f"late payment lost the race: {exc}")
    print(f"{late.id} is {late.status}; refunds issued: {len(gateway.refunds)}")

    refund = bookings.cancel(first.id)
    print(f"{first.id} cancelled {(SHOW_TIME - clock.now()) / 3600:.1f}h before showtime -> refund {refund}")
    print(f"seats free again: {','.join(show.available(clock.now()))}")
    for line in notifier.outbox():
        print(f"  notify {line}")

    leg = FlightLeg("LEG-1", "AI2841", "BLR", "DEL", SHOW_TIME, Money.of("4500.00"), (Cabin("business", 1, 2, "AC"),))
    _, _, flight = leg_as_show(leg)
    print(f"airline variant: {leg.flight_number} {leg.origin}->{leg.destination} is show {flight.id} with {len(flight.seats)} seats")


if __name__ == "__main__":
    main()
