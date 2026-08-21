"""The airline variant: a flight leg *is* a show, a cabin *is* a seat type.

This module is the whole answer to "now do it for flights". It builds catalog
objects; ``SeatLockService``, ``BookingService`` and both state machines are reused
unchanged, which is the point you want the interviewer to hear.
"""

from __future__ import annotations

from dataclasses import dataclass

from common import Money
from lld.movie_ticket_booking.models import Cinema, City, Screen, Seat, SeatType, Show

CABIN_SEAT_TYPES: dict[str, SeatType] = {
    "economy": SeatType.REGULAR,
    "premium_economy": SeatType.PREMIUM,
    "business": SeatType.RECLINER,
}


@dataclass(frozen=True, slots=True)
class Cabin:
    """A block of rows sold as one class of service."""

    name: str  # "economy", "business"
    first_row: int
    last_row: int
    letters: str = "ABCDEF"

    def seats(self) -> tuple[Seat, ...]:
        seat_type = CABIN_SEAT_TYPES[self.name]
        return tuple(
            Seat(number=f"{row}{letter}", row=str(row), type=seat_type)
            for row in range(self.first_row, self.last_row + 1)
            for letter in self.letters
        )


@dataclass(frozen=True, slots=True)
class FlightLeg:
    """One origin-to-destination hop of a flight on one date."""

    id: str
    flight_number: str
    origin: str
    destination: str
    departs_at: float
    base_fare: Money
    cabins: tuple[Cabin, ...]


def leg_as_show(leg: FlightLeg) -> tuple[City, Cinema, Show]:
    """Map a leg onto the catalog: origin airport = city, aircraft = screen, leg = show.

    A multi-leg itinerary is then an all-or-nothing hold across several shows, which is
    exactly ``SeatLockService.seats_locked`` with keys sorted across leg ids too.
    """
    seats = tuple(seat for cabin in leg.cabins for seat in cabin.seats())
    city = City(id=leg.origin, name=leg.origin)
    screen = Screen(id=f"{leg.flight_number}-cabin", cinema_id=leg.flight_number, name="cabin", seats=seats)
    cinema = Cinema(
        id=leg.flight_number, city_id=leg.origin, name=leg.flight_number, screens=(screen,)
    )
    show = Show.for_screen(
        show_id=leg.id,
        movie_id=f"{leg.origin}-{leg.destination}",
        screen=screen,
        starts_at=leg.departs_at,
        base_price=leg.base_fare,
    )
    return city, cinema, show
