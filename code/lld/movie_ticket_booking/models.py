"""Entities, value objects, enums and domain errors for the ticket booking system.

The two state machines that carry the interview live here: ``ShowSeat`` moves
AVAILABLE -> HELD -> BOOKED and ``Booking`` moves PENDING -> CONFIRMED / EXPIRED /
CANCELLED. Every transition is guarded; the locking that makes the guards safe under
concurrency is in ``services.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError

MAX_SEATS_PER_BOOKING = 10


# --8<-- [start:enums]
class SeatType(StrEnum):
    REGULAR = "regular"
    PREMIUM = "premium"
    RECLINER = "recliner"


class SeatStatus(StrEnum):
    AVAILABLE = "available"
    HELD = "held"  # reserved for one booking until hold_expires_at
    BOOKED = "booked"


class BookingStatus(StrEnum):
    PENDING = "pending"  # seats held, payment not captured yet
    CONFIRMED = "confirmed"
    EXPIRED = "expired"  # the hold TTL ran out before the payment landed
    CANCELLED = "cancelled"


class PaymentStatus(StrEnum):
    IN_FLIGHT = "in_flight"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class PaymentMethod(StrEnum):
    CARD = "card"
    UPI = "upi"
    WALLET = "wallet"


# --8<-- [end:enums]


# --8<-- [start:errors]
class SeatUnavailableError(ConflictError):
    """At least one requested seat is held or booked by somebody else."""


class HoldExpiredError(ConflictError):
    """The hold is gone by the time the payment callback arrived (the sweeper won)."""


class BookingStateError(InvalidStateError):
    """The booking is not in a state that allows this operation."""


class ShowNotFoundError(NotFoundError):
    """Unknown show, cinema, movie or seat id."""


class PaymentDeclinedError(ConflictError):
    """The gateway refused the charge; the hold survives so the user can retry."""


class PaymentInFlightError(ConflictError):
    """A payment with this idempotency key is still running; do not charge twice."""


# --8<-- [end:errors]


# --8<-- [start:catalog_entities]
@dataclass(frozen=True, slots=True)
class City:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Movie:
    id: str
    title: str
    language: str
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class Seat:
    """A physical seat in a screen. Immutable: its *availability* lives in ShowSeat."""

    number: str  # "A1", "H12"
    row: str
    type: SeatType = SeatType.REGULAR


@dataclass(frozen=True, slots=True)
class Screen:
    id: str
    cinema_id: str
    name: str
    seats: tuple[Seat, ...]


@dataclass(frozen=True, slots=True)
class Cinema:
    id: str
    city_id: str
    name: str
    screens: tuple[Screen, ...]


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    email: str


# --8<-- [end:catalog_entities]


# --8<-- [start:show_seat]
@dataclass(slots=True)
class ShowSeat:
    """One seat *for one show* - the contended row in the real database.

    ``held_by`` and ``booking_id`` are both booking ids; keeping them in separate
    fields makes "held by me" and "already sold to me" impossible to confuse.
    """

    show_id: str
    seat: Seat
    status: SeatStatus = SeatStatus.AVAILABLE
    held_by: str | None = None
    hold_expires_at: float | None = None
    booking_id: str | None = None

    @property
    def number(self) -> str:
        return self.seat.number

    def is_takeable(self, now: float) -> bool:
        """Free, or held by a hold whose TTL has already run out (lazy expiry)."""
        if self.status is SeatStatus.AVAILABLE:
            return True
        return self.status is SeatStatus.HELD and (self.hold_expires_at or 0.0) <= now

    def hold(self, booking_id: str, expires_at: float) -> None:
        """AVAILABLE -> HELD. Callers must have checked ``is_takeable`` under the seat lock."""
        self.status = SeatStatus.HELD
        self.held_by = booking_id
        self.hold_expires_at = expires_at
        self.booking_id = None

    def book(self, booking_id: str) -> None:
        """HELD -> BOOKED, only for the booking that owns the hold."""
        if self.status is not SeatStatus.HELD or self.held_by != booking_id:
            raise InvalidStateError(
                f"seat {self.number} is {self.status} (held_by={self.held_by}), "
                f"cannot be booked by {booking_id}"
            )
        self.status = SeatStatus.BOOKED
        self.booking_id = booking_id
        self.held_by = None
        self.hold_expires_at = None

    def release(self) -> None:
        """HELD or BOOKED -> AVAILABLE (timeout, user cancel, refund)."""
        self.status = SeatStatus.AVAILABLE
        self.held_by = None
        self.hold_expires_at = None
        self.booking_id = None


# --8<-- [end:show_seat]


# --8<-- [start:show]
@dataclass(slots=True)
class Show:
    """A movie on a screen at a time, with its own copy of the seat map.

    ``base_price`` is the regular-seat price; the pricing strategy scales it per seat.
    """

    id: str
    movie_id: str
    cinema_id: str
    screen_id: str
    starts_at: float  # epoch seconds, from the injected Clock
    base_price: Money
    seats: dict[str, ShowSeat] = field(default_factory=dict)

    @classmethod
    def for_screen(
        cls, show_id: str, movie_id: str, screen: Screen, starts_at: float, base_price: Money
    ) -> Show:
        show = cls(show_id, movie_id, screen.cinema_id, screen.id, starts_at, base_price)
        show.seats = {s.number: ShowSeat(show_id, s) for s in screen.seats}
        return show

    def seat(self, number: str) -> ShowSeat:
        try:
            return self.seats[number]
        except KeyError:
            raise ShowNotFoundError(f"show {self.id} has no seat {number!r}") from None

    def all_seats(self) -> list[ShowSeat]:
        return list(self.seats.values())

    def available(self, now: float) -> list[str]:
        return sorted(s.number for s in self.seats.values() if s.is_takeable(now))

    def seat_map(self, now: float) -> dict[str, SeatStatus]:
        """What the browser renders: the *effective* status, expired holds shown as free."""
        return {
            number: SeatStatus.AVAILABLE if s.is_takeable(now) else s.status
            for number, s in sorted(self.seats.items())
        }


# --8<-- [end:show]


# --8<-- [start:booking]
BOOKING_TRANSITIONS: dict[BookingStatus, frozenset[BookingStatus]] = {
    BookingStatus.PENDING: frozenset(
        {BookingStatus.CONFIRMED, BookingStatus.EXPIRED, BookingStatus.CANCELLED}
    ),
    BookingStatus.CONFIRMED: frozenset({BookingStatus.CANCELLED}),
    BookingStatus.EXPIRED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
}


@dataclass(slots=True)
class Booking:
    id: str
    show_id: str
    user_id: str
    seat_numbers: tuple[str, ...]
    amount: Money
    created_at: float
    hold_expires_at: float
    status: BookingStatus = BookingStatus.PENDING
    payment_id: str | None = None
    refunded: Money | None = None

    def transition_to(self, target: BookingStatus) -> None:
        """The whole Booking state machine in four lines - no if/elif ladder."""
        if target not in BOOKING_TRANSITIONS[self.status]:
            raise BookingStateError(f"booking {self.id}: {self.status} -> {target} is not allowed")
        self.status = target

    def seconds_left(self, now: float) -> float:
        return max(0.0, self.hold_expires_at - now)


@dataclass(slots=True)
class Payment:
    id: str
    booking_id: str
    amount: Money
    method: PaymentMethod
    idempotency_key: str
    status: PaymentStatus = PaymentStatus.IN_FLIGHT


# --8<-- [end:booking]


def validate_seat_request(show: Show, seat_numbers: tuple[str, ...]) -> tuple[str, ...]:
    """Reject empty, oversized, duplicated and unknown seat requests before any lock is taken."""
    if not seat_numbers:
        raise ValidationError("select at least one seat")
    if len(seat_numbers) > MAX_SEATS_PER_BOOKING:
        raise ValidationError(f"at most {MAX_SEATS_PER_BOOKING} seats per booking")
    if len(set(seat_numbers)) != len(seat_numbers):
        raise ValidationError(f"duplicate seats in request: {seat_numbers}")
    for number in seat_numbers:
        show.seat(number)  # raises ShowNotFoundError for an unknown seat
    return tuple(seat_numbers)
