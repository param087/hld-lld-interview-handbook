"""Entities, value objects, enums and domain errors for the hotel.

The value object that carries the whole problem is ``DateRange``: a **half-open**
interval ``[start, end)`` whose ``end`` is the checkout date. Half-open is what makes
"leaves Friday, arrives Friday" a non-overlap instead of an argument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class RoomType(StrEnum):
    SINGLE = "single"
    DOUBLE = "double"
    DELUXE = "deluxe"
    SUITE = "suite"


class RoomStatus(StrEnum):
    AVAILABLE = "available"  # clean and sellable
    OCCUPIED = "occupied"
    CLEANING = "cleaning"  # vacated, waiting for housekeeping
    OUT_OF_SERVICE = "out_of_service"


class ReservationStatus(StrEnum):
    PENDING = "pending"  # inventory held, payment not captured
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class StaffRole(StrEnum):
    RECEPTIONIST = "receptionist"
    HOUSEKEEPER = "housekeeper"
    MANAGER = "manager"


class TaskKind(StrEnum):
    TURNDOWN = "turndown"
    DEEP_CLEAN = "deep_clean"
    MAINTENANCE = "maintenance"


class PaymentMethod(StrEnum):
    CARD = "card"
    CASH = "cash"


# --8<-- [end:enums]


# --8<-- [start:errors]
class NoAvailabilityError(ConflictError):
    """Not enough rooms of that type are free for every night of the stay."""


class NoRoomReadyError(ConflictError):
    """The type is sold correctly but no physical room of it is clean right now."""


class ReservationStateError(InvalidStateError):
    """The reservation is not in a state that allows this operation."""


class RoomStateError(InvalidStateError):
    """The room is not in a state that allows this operation."""


class UnknownReservationError(NotFoundError):
    """Unknown reservation, room or guest id."""


class PaymentDeclinedError(ConflictError):
    """The gateway refused the charge; the hold survives so the guest can retry."""


# --8<-- [end:errors]


# --8<-- [start:date_range]
@dataclass(frozen=True, slots=True, order=True)
class DateRange:
    """A stay as a half-open interval: arrive on ``start``, leave on ``end``.

    >>> DateRange(date(2026, 3, 1), date(2026, 3, 3)).nights_count
    2
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValidationError(f"checkout {self.end} must be after check-in {self.start}")

    @property
    def nights_count(self) -> int:
        return (self.end - self.start).days

    def nights(self) -> list[date]:
        """The nights actually slept: ``end`` is a departure, never a night."""
        return [self.start + timedelta(days=i) for i in range(self.nights_count)]

    def overlaps(self, other: DateRange) -> bool:
        """Half-open overlap: touching intervals do not overlap."""
        return self.start < other.end and other.start < self.end

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


@dataclass(frozen=True, slots=True)
class RoomRequest:
    """How many rooms of one type a reservation asks for."""

    room_type: RoomType
    count: int = 1

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValidationError("a room request needs at least one room")


# --8<-- [end:date_range]


# --8<-- [start:entities]
@dataclass(frozen=True, slots=True)
class Guest:
    id: str
    name: str
    email: str


@dataclass(frozen=True, slots=True)
class Staff:
    id: str
    name: str
    role: StaffRole


@dataclass(slots=True)
class Room:
    """A physical room. Its status is about *housekeeping*, not about sales.

    Sellability for a future date lives in ``AvailabilityService``; this status
    answers "can a guest walk into it right now".
    """

    number: str
    floor: int
    type: RoomType
    status: RoomStatus = RoomStatus.AVAILABLE
    reservation_id: str | None = None

    def occupy(self, reservation_id: str) -> None:
        if self.status is not RoomStatus.AVAILABLE:
            raise RoomStateError(f"room {self.number} is {self.status}, not available")
        self.status = RoomStatus.OCCUPIED
        self.reservation_id = reservation_id

    def vacate(self) -> None:
        if self.status is not RoomStatus.OCCUPIED:
            raise RoomStateError(f"room {self.number} is {self.status}, not occupied")
        self.status = RoomStatus.CLEANING
        self.reservation_id = None

    def mark_clean(self) -> None:
        if self.status is not RoomStatus.CLEANING:
            raise RoomStateError(f"room {self.number} is {self.status}, not cleaning")
        self.status = RoomStatus.AVAILABLE

    def unassign(self) -> None:
        """Roll back an assignment the guest never received (partial check-in failure)."""
        if self.status is not RoomStatus.OCCUPIED:
            raise RoomStateError(f"room {self.number} is {self.status}, not occupied")
        self.status = RoomStatus.AVAILABLE
        self.reservation_id = None


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    description: str
    amount: Money


@dataclass(frozen=True, slots=True)
class Invoice:
    id: str
    reservation_id: str
    lines: tuple[InvoiceLine, ...]
    tax: Money
    total: Money

    def subtotal(self) -> Money:
        return self.total - self.tax


@dataclass(slots=True)
class HousekeepingTask:
    id: str
    room_number: str
    kind: TaskKind
    created_at: float
    assigned_to: str | None = None
    done: bool = False


@dataclass(slots=True)
class Payment:
    id: str
    reservation_id: str
    amount: Money
    method: PaymentMethod
    idempotency_key: str
    captured: bool = False


# --8<-- [end:entities]


# --8<-- [start:reservation]
RESERVATION_TRANSITIONS: dict[ReservationStatus, frozenset[ReservationStatus]] = {
    ReservationStatus.PENDING: frozenset(
        {ReservationStatus.CONFIRMED, ReservationStatus.CANCELLED}
    ),
    ReservationStatus.CONFIRMED: frozenset(
        {ReservationStatus.CHECKED_IN, ReservationStatus.CANCELLED, ReservationStatus.NO_SHOW}
    ),
    ReservationStatus.CHECKED_IN: frozenset({ReservationStatus.CHECKED_OUT}),
    ReservationStatus.CHECKED_OUT: frozenset(),
    ReservationStatus.CANCELLED: frozenset(),
    ReservationStatus.NO_SHOW: frozenset(),
}


@dataclass(slots=True)
class Reservation:
    id: str
    guest_id: str
    stay: DateRange
    rooms: tuple[RoomRequest, ...]
    amount: Money
    status: ReservationStatus = ReservationStatus.PENDING
    assigned_rooms: tuple[str, ...] = ()
    extras: list[InvoiceLine] = field(default_factory=list)
    payment_id: str | None = None
    refunded: Money | None = None

    @property
    def room_count(self) -> int:
        return sum(r.count for r in self.rooms)

    def transition_to(self, target: ReservationStatus) -> None:
        """The entire reservation state machine, table-driven."""
        if target not in RESERVATION_TRANSITIONS[self.status]:
            raise ReservationStateError(
                f"reservation {self.id}: {self.status} -> {target} is not allowed"
            )
        self.status = target


# --8<-- [end:reservation]
