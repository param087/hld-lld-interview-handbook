"""Entities, value objects, the trip transition table and domain errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import cos, hypot, radians

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError

KM_PER_DEGREE = 111.0


# --8<-- [start:enums]
class RideType(StrEnum):
    ECONOMY = "economy"
    COMFORT = "comfort"
    XL = "xl"
    BLACK = "black"


class DriverStatus(StrEnum):
    OFFLINE = "offline"
    AVAILABLE = "available"  # in the index, can be offered a trip
    OFFERED = "offered"  # holds one live lease
    ON_TRIP = "on_trip"


class TripStatus(StrEnum):
    REQUESTED = "requested"  # matching in progress
    MATCHED = "matched"  # a driver accepted and is on the way
    ARRIVED = "arrived"  # driver is at the pickup point
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_DRIVER = "no_driver"  # the offer cascade ran out of candidates


class OfferStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    VOIDED = "voided"  # the rider cancelled underneath the offer


#: The whole trip lifecycle in one dict. ``TripService.transition`` is the only
#: caller, and the state diagram on the page is a drawing of exactly this table.
TRIP_TRANSITIONS: dict[TripStatus, frozenset[TripStatus]] = {
    TripStatus.REQUESTED: frozenset({TripStatus.MATCHED, TripStatus.CANCELLED, TripStatus.NO_DRIVER}),
    TripStatus.MATCHED: frozenset({TripStatus.ARRIVED, TripStatus.CANCELLED}),
    TripStatus.ARRIVED: frozenset({TripStatus.IN_PROGRESS, TripStatus.CANCELLED}),
    TripStatus.IN_PROGRESS: frozenset({TripStatus.COMPLETED}),
    TripStatus.COMPLETED: frozenset(),
    TripStatus.CANCELLED: frozenset(),
    TripStatus.NO_DRIVER: frozenset(),
}

#: After this point a driver has already burned fuel getting to the rider.
CHARGEABLE_CANCEL_FROM = frozenset({TripStatus.MATCHED, TripStatus.ARRIVED})
# --8<-- [end:enums]


# --8<-- [start:errors]
class NoDriverAvailableError(ConflictError):
    """Nobody eligible is available inside the search radius."""


class TripStateError(InvalidStateError):
    """The trip is not in a state that allows this transition."""


class OfferStateError(InvalidStateError):
    """The offer expired, was already answered, or names a different driver."""


class UnknownTripError(NotFoundError):
    """No trip with that id."""


class UnknownDriverError(NotFoundError):
    """No driver with that id."""


# --8<-- [end:errors]


# --8<-- [start:values]
@dataclass(frozen=True, slots=True)
class Location:
    lat: float
    lon: float

    def distance_km(self, other: Location) -> float:
        """Equirectangular approximation: accurate enough inside one city, and cheap."""
        mean_lat = radians((self.lat + other.lat) / 2)
        dx = (other.lon - self.lon) * cos(mean_lat) * KM_PER_DEGREE
        dy = (other.lat - self.lat) * KM_PER_DEGREE
        return hypot(dx, dy)


@dataclass(frozen=True, slots=True)
class RideTypeSpec:
    """The commercial rules of one ride class: what it costs and what it needs."""

    ride_type: RideType
    base: Money
    per_km: Money
    per_minute: Money
    minimum: Money
    seats: int


class RideTypeFactory:
    """Factory: a request carries the string ``"xl"``, not a pricing table."""

    _registry: dict[RideType, RideTypeSpec] = {
        RideType.ECONOMY: RideTypeSpec(
            RideType.ECONOMY, Money.of("2.00"), Money.of("0.90"), Money.of("0.20"), Money.of("4.00"), 4
        ),
        RideType.COMFORT: RideTypeSpec(
            RideType.COMFORT, Money.of("3.00"), Money.of("1.20"), Money.of("0.25"), Money.of("6.00"), 4
        ),
        RideType.XL: RideTypeSpec(
            RideType.XL, Money.of("4.00"), Money.of("1.60"), Money.of("0.30"), Money.of("8.00"), 6
        ),
        RideType.BLACK: RideTypeSpec(
            RideType.BLACK, Money.of("6.00"), Money.of("2.40"), Money.of("0.45"), Money.of("12.00"), 4
        ),
    }

    @classmethod
    def spec(cls, ride_type: RideType | str) -> RideTypeSpec:
        try:
            return cls._registry[RideType(ride_type)]
        except ValueError as exc:
            raise ValidationError(f"unknown ride type: {ride_type!r}") from exc

    @classmethod
    def all_types(cls) -> tuple[RideType, ...]:
        return tuple(cls._registry)


@dataclass(frozen=True, slots=True)
class Vehicle:
    plate: str
    model: str
    seats: int
    ride_types: frozenset[RideType]

    def serves(self, ride_type: RideType) -> bool:
        return ride_type in self.ride_types


@dataclass(frozen=True, slots=True)
class DriverSnapshot:
    """An immutable read of one driver at ranking time.

    Matching strategies see only this, never a live ``Driver``, so a strategy
    physically cannot mutate dispatch state or observe a half-written field.
    """

    driver_id: str
    location: Location
    rating: float
    trips_today: int

    def pickup_km(self, pickup: Location) -> float:
        return self.location.distance_km(pickup)


@dataclass(frozen=True, slots=True)
class Fare:
    """An itemised fare. The rider sees why the number is the number."""

    base: Money
    distance: Money
    time: Money
    surge_multiplier: float
    total: Money

    def __str__(self) -> str:
        return (
            f"{self.total} = ({self.base} base + {self.distance} distance + {self.time} time)"
            f" x {self.surge_multiplier:.2f} surge"
        )


# --8<-- [end:values]


# --8<-- [start:entities]
@dataclass(slots=True)
class Rider:
    id: str
    name: str
    rating: float = 5.0


@dataclass(slots=True)
class Driver:
    id: str
    name: str
    vehicle: Vehicle
    location: Location
    rating: float = 5.0
    ratings_count: int = 0
    trips_today: int = 0
    status: DriverStatus = DriverStatus.OFFLINE
    current_trip_id: str | None = None

    def is_available(self) -> bool:
        return self.status is DriverStatus.AVAILABLE


@dataclass(frozen=True, slots=True)
class RideRequest:
    id: str
    rider_id: str
    pickup: Location
    dropoff: Location
    ride_type: RideType
    requested_at: float

    @property
    def straight_line_km(self) -> float:
        return self.pickup.distance_km(self.dropoff)


@dataclass(slots=True)
class Trip:
    id: str
    request: RideRequest
    status: TripStatus = TripStatus.REQUESTED
    driver_id: str | None = None
    matched_at: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    distance_km: float = 0.0
    fare: Fare | None = None
    cancellation_fee: Money | None = None

    def can_move_to(self, target: TripStatus) -> bool:
        return target in TRIP_TRANSITIONS[self.status]

    def minutes(self) -> float:
        if self.started_at is None or self.ended_at is None:
            return 0.0
        return max(0.0, (self.ended_at - self.started_at) / 60.0)


@dataclass(slots=True)
class DriverOffer:
    """A time-boxed lease on one driver. Exactly one can be live per driver."""

    id: str
    trip_id: str
    driver_id: str
    created_at: float
    expires_at: float
    status: OfferStatus = OfferStatus.PENDING

    def is_live(self, now: float) -> bool:
        return self.status is OfferStatus.PENDING and now < self.expires_at


@dataclass(slots=True)
class Payment:
    id: str
    trip_id: str
    amount: Money
    captured: bool = False


@dataclass(frozen=True, slots=True)
class Rating:
    trip_id: str
    driver_id: str
    stars: int
    comment: str = ""

    def __post_init__(self) -> None:
        if not 1 <= self.stars <= 5:
            raise ValidationError("stars must be between 1 and 5")


# --8<-- [end:entities]
