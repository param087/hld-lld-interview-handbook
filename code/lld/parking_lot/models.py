"""Entities, value objects, enums and domain errors for the parking lot.

No business logic lives here beyond simple invariants; the behaviour is in
``services.py`` and the pluggable policies are in ``strategies.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class VehicleType(StrEnum):
    MOTORCYCLE = "motorcycle"
    CAR = "car"
    TRUCK = "truck"
    ELECTRIC_CAR = "electric_car"


class SpotType(StrEnum):
    MOTORCYCLE = "motorcycle"
    COMPACT = "compact"
    LARGE = "large"
    ELECTRIC = "electric"  # compact spot with a charger


class SpotStatus(StrEnum):
    FREE = "free"
    OCCUPIED = "occupied"
    OUT_OF_SERVICE = "out_of_service"


class TicketStatus(StrEnum):
    ACTIVE = "active"  # vehicle is inside
    PAYING = "paying"  # reserved by an exit gate while the payment is in flight
    PAID = "paid"  # fee settled at the exit gate, spot released
    LOST = "lost"  # settled via the lost-ticket flow


class PaymentMethod(StrEnum):
    CASH = "cash"
    CARD = "card"


# --8<-- [end:enums]


# --8<-- [start:errors]
class LotFullError(ConflictError):
    """No spot of an acceptable type is free anywhere in the lot."""


class InvalidTicketError(NotFoundError):
    """Unknown ticket id (or plate, for the lost-ticket flow)."""


class TicketStateError(InvalidStateError):
    """The ticket is not in a state that allows the operation (e.g. paid twice)."""


class PaymentDeclinedError(ConflictError):
    """The payment processor refused the charge; the ticket stays active."""


# --8<-- [end:errors]


# --8<-- [start:vehicles]
class Vehicle(ABC):
    """A vehicle knows which spot types can hold it, in order of preference.

    Polymorphism replaces an if/elif chain on vehicle type in the allocator:
    adding a new vehicle class never touches the lot code (open/closed).
    """

    def __init__(self, plate: str) -> None:
        if not plate or not plate.strip():
            raise ValidationError("plate must be non-empty")
        self.plate = plate.strip().upper()

    @property
    @abstractmethod
    def vehicle_type(self) -> VehicleType: ...

    @property
    @abstractmethod
    def allowed_spot_types(self) -> tuple[SpotType, ...]:
        """Acceptable spot types, most preferred first."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.plate!r})"


class Motorcycle(Vehicle):
    vehicle_type = VehicleType.MOTORCYCLE
    allowed_spot_types = (SpotType.MOTORCYCLE, SpotType.COMPACT, SpotType.LARGE)


class Car(Vehicle):
    vehicle_type = VehicleType.CAR
    allowed_spot_types = (SpotType.COMPACT, SpotType.LARGE)


class Truck(Vehicle):
    vehicle_type = VehicleType.TRUCK
    allowed_spot_types = (SpotType.LARGE,)


class ElectricCar(Vehicle):
    vehicle_type = VehicleType.ELECTRIC_CAR
    allowed_spot_types = (SpotType.ELECTRIC, SpotType.COMPACT, SpotType.LARGE)


class VehicleFactory:
    """Factory Method: the gate turns raw input ("car", "KA01AB1234") into an object."""

    _registry: dict[VehicleType, type[Vehicle]] = {
        VehicleType.MOTORCYCLE: Motorcycle,
        VehicleType.CAR: Car,
        VehicleType.TRUCK: Truck,
        VehicleType.ELECTRIC_CAR: ElectricCar,
    }

    @classmethod
    def create(cls, vehicle_type: VehicleType | str, plate: str) -> Vehicle:
        try:
            klass = cls._registry[VehicleType(vehicle_type)]
        except ValueError as exc:
            raise ValidationError(f"unknown vehicle type: {vehicle_type!r}") from exc
        return klass(plate)


# --8<-- [end:vehicles]


# --8<-- [start:entities]
@dataclass(slots=True)
class ParkingSpot:
    id: str  # e.g. "F1-C07"
    floor: int
    type: SpotType
    status: SpotStatus = SpotStatus.FREE
    vehicle: Vehicle | None = None

    def is_free(self) -> bool:
        return self.status is SpotStatus.FREE

    def assign(self, vehicle: Vehicle) -> None:
        if not self.is_free():
            raise ConflictError(f"spot {self.id} is {self.status}")
        self.vehicle = vehicle
        self.status = SpotStatus.OCCUPIED

    def release(self) -> None:
        if self.status is not SpotStatus.OCCUPIED:
            raise InvalidStateError(f"spot {self.id} is not occupied")
        self.vehicle = None
        self.status = SpotStatus.FREE


@dataclass(slots=True)
class Ticket:
    id: str
    plate: str
    vehicle_type: VehicleType
    spot_id: str
    floor: int
    entry_time: float  # epoch seconds, from the injected Clock
    exit_time: float | None = None
    fee: Money | None = None
    status: TicketStatus = TicketStatus.ACTIVE

    def duration_seconds(self, now: float) -> float:
        return max(0.0, (self.exit_time or now) - self.entry_time)


@dataclass(frozen=True, slots=True)
class Payment:
    id: str
    ticket_id: str
    amount: Money
    method: PaymentMethod
    paid_at: float


@dataclass(frozen=True, slots=True)
class FloorAvailability:
    """What a display board shows for one floor: free spots per type."""

    floor: int
    free: dict[SpotType, int] = field(default_factory=dict)

    def total_free(self) -> int:
        return sum(self.free.values())


# --8<-- [end:entities]
