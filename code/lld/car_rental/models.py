"""Entities, value objects, enums and domain errors for the car rental system.

Behaviour that needs a lock lives in ``services.py``; the pluggable pricing
policies live in ``strategies.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class VehicleType(StrEnum):
    """The commercial class a customer books. Cars are booked by type, never by plate."""

    ECONOMY = "economy"
    SEDAN = "sedan"
    SUV = "suv"
    VAN = "van"
    LUXURY = "luxury"


class VehicleStatus(StrEnum):
    AVAILABLE = "available"  # on the lot, can be handed over
    RENTED = "rented"  # checked out of the fleet pool
    MAINTENANCE = "maintenance"  # in the workshop, invisible to the desk
    RETIRED = "retired"  # sold or written off; never counted again


class ReservationStatus(StrEnum):
    RESERVED = "reserved"  # holds one counted slot of its type at the branch
    PICKED_UP = "picked_up"  # pinned to a plate; the slot is now a calendar block
    RETURNED = "returned"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class AddOnType(StrEnum):
    GPS = "gps"
    CHILD_SEAT = "child_seat"
    INSURANCE = "insurance"


class PaymentMethod(StrEnum):
    CARD = "card"
    CASH = "cash"


#: Which classes may be handed over when the booked class is out at the desk.
#: A van seats seven, so nothing substitutes for it; a luxury downgrade is a
#: refund conversation, not an upgrade, so luxury only ever gets luxury.
UPGRADE_LADDER: dict[VehicleType, tuple[VehicleType, ...]] = {
    VehicleType.ECONOMY: (VehicleType.ECONOMY, VehicleType.SEDAN, VehicleType.SUV),
    VehicleType.SEDAN: (VehicleType.SEDAN, VehicleType.SUV, VehicleType.LUXURY),
    VehicleType.SUV: (VehicleType.SUV, VehicleType.LUXURY),
    VehicleType.VAN: (VehicleType.VAN,),
    VehicleType.LUXURY: (VehicleType.LUXURY,),
}
# --8<-- [end:enums]


# --8<-- [start:errors]
class NoVehicleAvailableError(ConflictError):
    """No car of an acceptable class is free at that branch for those dates."""


class OverlappingReservationError(ConflictError):
    """A calendar block for this vehicle already covers part of the requested range."""


class ReservationStateError(InvalidStateError):
    """The reservation is not in a state that allows the operation (cancel after pickup)."""


class UnknownReservationError(NotFoundError):
    """No reservation with that id."""


class UnknownVehicleError(NotFoundError):
    """No vehicle with that plate at this branch."""


class UnknownBranchError(NotFoundError):
    """No branch with that id."""


# --8<-- [end:errors]


# --8<-- [start:daterange]
@dataclass(frozen=True, slots=True, order=True)
class DateRange:
    """A **half-open** interval ``[start, end)`` over calendar days.

    Half-open is the whole trick: a rental that ends on the 4th does *not*
    occupy the 4th, so the next customer can pick the same car up that morning.
    Two ranges overlap iff ``a.start < b.end and b.start < a.end`` -- one line,
    no off-by-one, and it is the same predicate a SQL ``tstzrange && tstzrange``
    exclusion constraint would apply.
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValidationError(f"end {self.end} must be after start {self.start}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def overlaps(self, other: DateRange) -> bool:
        return self.start < other.end and other.start < self.end

    def extended_to(self, new_end: date) -> DateRange:
        return DateRange(self.start, max(new_end, self.end))

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


# --8<-- [end:daterange]


# --8<-- [start:calendar]
class VehicleCalendar:
    """The blocked date ranges of one vehicle: rentals and maintenance windows.

    Deliberately *not* thread-safe. ``Branch`` owns the only lock and calls this
    class under it, so a vehicle's calendar and the branch's booking ledger can
    never disagree with each other.
    """

    __slots__ = ("_blocks",)

    def __init__(self) -> None:
        self._blocks: list[tuple[str, DateRange]] = []

    def blocks(self) -> list[tuple[str, DateRange]]:
        return list(self._blocks)

    def conflict(self, period: DateRange) -> str | None:
        """Label of the first block overlapping ``period``, or None."""
        for label, blocked in self._blocks:
            if blocked.overlaps(period):
                return label
        return None

    def is_free(self, period: DateRange) -> bool:
        return self.conflict(period) is None

    def block(self, label: str, period: DateRange) -> None:
        clash = self.conflict(period)
        if clash is not None:
            raise OverlappingReservationError(f"{period} overlaps existing block {clash}")
        self._blocks.append((label, period))

    def block_or_merge(self, label: str, period: DateRange) -> str:
        """Block ``period``, or grow the block it clashes with so it covers ``period``.

        A car that comes back damaged inside a window the workshop already holds
        needs one continuous slot, not two overlapping ones. Returns the label of
        the block that ended up covering the range.
        """
        clash = self.conflict(period)
        if clash is None:
            self._blocks.append((label, period))
            return label
        for index, (name, blocked) in enumerate(self._blocks):
            if name == clash:
                merged = DateRange(min(blocked.start, period.start), max(blocked.end, period.end))
                self._blocks[index] = (name, merged)
                return name
        return label

    def unblock(self, label: str) -> None:
        self._blocks = [entry for entry in self._blocks if entry[0] != label]

    def extend(self, label: str, new_end: date) -> list[str]:
        """Grow a block because the car came back late.

        Returns the labels the grown block now runs into -- the late-return
        cascade. The physical car really is late, so the extension always wins;
        the caller decides what to do with the bookings it displaced.
        """
        for index, (name, period) in enumerate(self._blocks):
            if name != label:
                continue
            grown = period.extended_to(new_end)
            self._blocks[index] = (name, grown)
            return [other for other, blk in self._blocks if other != label and blk.overlaps(grown)]
        raise NotFoundError(f"no calendar block labelled {label}")


# --8<-- [end:calendar]


# --8<-- [start:vehicles]
class Vehicle(ABC):
    """One physical car. Subclasses fix the commercial class and its capacity.

    The desk never asks "what type are you?" in an if/elif ladder: it asks the
    fleet pool for a class, and the class comes from the object itself.
    """

    def __init__(
        self, plate: str, branch_id: str, odometer_km: int = 0, fuel_eighths: int = 8
    ) -> None:
        if not plate or not plate.strip():
            raise ValidationError("plate must be non-empty")
        if not 0 <= fuel_eighths <= 8:
            raise ValidationError("fuel is measured in eighths of a tank (0-8)")
        self.plate = plate.strip().upper()
        self.branch_id = branch_id
        self.odometer_km = odometer_km
        self.fuel_eighths = fuel_eighths
        self.status = VehicleStatus.AVAILABLE
        self.calendar = VehicleCalendar()

    @property
    @abstractmethod
    def vehicle_type(self) -> VehicleType: ...

    @property
    @abstractmethod
    def seats(self) -> int: ...

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.plate!r})"


class EconomyCar(Vehicle):
    vehicle_type = VehicleType.ECONOMY
    seats = 4


class Sedan(Vehicle):
    vehicle_type = VehicleType.SEDAN
    seats = 5


class Suv(Vehicle):
    vehicle_type = VehicleType.SUV
    seats = 5


class Van(Vehicle):
    vehicle_type = VehicleType.VAN
    seats = 7


class LuxuryCar(Vehicle):
    vehicle_type = VehicleType.LUXURY
    seats = 4


class VehicleFactory:
    """Factory Method: fleet import files carry a class string, not a Python type."""

    _registry: dict[VehicleType, type[Vehicle]] = {
        VehicleType.ECONOMY: EconomyCar,
        VehicleType.SEDAN: Sedan,
        VehicleType.SUV: Suv,
        VehicleType.VAN: Van,
        VehicleType.LUXURY: LuxuryCar,
    }

    @classmethod
    def create(cls, vehicle_type: VehicleType | str, plate: str, branch_id: str, **kwargs: int) -> Vehicle:
        try:
            klass = cls._registry[VehicleType(vehicle_type)]
        except ValueError as exc:
            raise ValidationError(f"unknown vehicle type: {vehicle_type!r}") from exc
        return klass(plate, branch_id, **kwargs)


# --8<-- [end:vehicles]


# --8<-- [start:entities]
@dataclass(frozen=True, slots=True)
class Customer:
    id: str
    name: str
    licence_number: str


@dataclass(slots=True)
class Reservation:
    """Booked by *class* and dates; pinned to a plate only at pickup."""

    id: str
    customer_id: str
    vehicle_type: VehicleType
    pickup_branch: str
    dropoff_branch: str
    period: DateRange
    add_ons: tuple[AddOnType, ...] = ()
    status: ReservationStatus = ReservationStatus.RESERVED
    plate: str | None = None
    handed_over_type: VehicleType | None = None
    pickup_odometer: int | None = None
    pickup_fuel: int | None = None
    return_date: date | None = None
    return_odometer: int | None = None
    return_fuel: int | None = None

    @property
    def is_one_way(self) -> bool:
        return self.pickup_branch != self.dropoff_branch

    @property
    def late_days(self) -> int:
        if self.return_date is None:
            return 0
        return max(0, (self.return_date - self.period.end).days)

    @property
    def kilometres_driven(self) -> int:
        if self.pickup_odometer is None or self.return_odometer is None:
            return 0
        return max(0, self.return_odometer - self.pickup_odometer)


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    label: str
    amount: Money


def total_of(lines: Iterable[InvoiceLine]) -> Money:
    total = Money(0)
    for line in lines:
        total = total + line.amount
    return total


@dataclass(frozen=True, slots=True)
class Invoice:
    id: str
    reservation_id: str
    lines: tuple[InvoiceLine, ...] = ()

    @property
    def total(self) -> Money:
        return total_of(self.lines)


@dataclass(frozen=True, slots=True)
class Payment:
    id: str
    invoice_id: str
    amount: Money
    method: PaymentMethod
    paid_at: float


@dataclass(frozen=True, slots=True)
class MaintenanceRecord:
    id: str
    plate: str
    period: DateRange
    reason: str


# --8<-- [end:entities]
