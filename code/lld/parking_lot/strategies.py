"""Pluggable policies: how to price a stay and how to pick a spot (Strategy pattern)."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Protocol

from common import Money, ValidationError
from lld.parking_lot.models import ParkingSpot, SpotType, Vehicle, VehicleType

if TYPE_CHECKING:
    from lld.parking_lot.services import ParkingFloor


# --8<-- [start:pricing]
class PricingStrategy(Protocol):
    """Turns a stay into money. Implementations are stateless and thread-safe."""

    def calculate(self, vehicle_type: VehicleType, duration_seconds: float) -> Money: ...


DEFAULT_FLAT_RATE = Money.of("10.00")
DEFAULT_DAILY_CAP = Money.of("20.00")
DEFAULT_HOURLY_RATES: dict[VehicleType, Money] = {
    VehicleType.MOTORCYCLE: Money.of("1.00"),
    VehicleType.CAR: Money.of("3.00"),
    VehicleType.ELECTRIC_CAR: Money.of("3.50"),
    VehicleType.TRUCK: Money.of("6.00"),
}


class HourlyPricing:
    """Charge per started hour; a free grace period (default 15 min) costs nothing."""

    def __init__(
        self,
        rates: dict[VehicleType, Money] | None = None,
        grace_seconds: int = 15 * 60,
    ) -> None:
        self._rates = rates or DEFAULT_HOURLY_RATES
        self._grace = grace_seconds

    def calculate(self, vehicle_type: VehicleType, duration_seconds: float) -> Money:
        if duration_seconds < 0:
            raise ValidationError("duration cannot be negative")
        if duration_seconds <= self._grace:
            return Money(0)
        hours = math.ceil(duration_seconds / 3600)
        return self._rates[vehicle_type] * hours


class FlatRatePricing:
    """One price per visit regardless of duration (event parking)."""

    def __init__(self, rate: Money = DEFAULT_FLAT_RATE) -> None:
        self._rate = rate

    def calculate(self, vehicle_type: VehicleType, duration_seconds: float) -> Money:
        return self._rate


class DailyCapPricing:
    """Hourly pricing, but no day costs more than the cap (airport parking)."""

    def __init__(self, hourly: HourlyPricing, daily_cap: Money = DEFAULT_DAILY_CAP) -> None:
        self._hourly = hourly
        self._cap = daily_cap

    def calculate(self, vehicle_type: VehicleType, duration_seconds: float) -> Money:
        full_days, remainder = divmod(duration_seconds, 86_400)
        remainder_fee = self._hourly.calculate(vehicle_type, remainder)
        capped_remainder = min(remainder_fee, self._cap)
        return self._cap * int(full_days) + capped_remainder


# --8<-- [end:pricing]


# --8<-- [start:allocation]
class SpotAllocationStrategy(Protocol):
    """Chooses a spot for a vehicle. Must only *select*; the floor does the locking."""

    def choose(self, floors: list[ParkingFloor], vehicle: Vehicle) -> ParkingSpot | None: ...


class NearestFirstAllocation:
    """Lowest floor first, then the vehicle's most preferred spot type, then lowest spot id.

    A motorcycle therefore takes a motorcycle spot before it falls back to a
    compact one — cheap spots are not wasted on small vehicles.
    """

    def choose(self, floors: list[ParkingFloor], vehicle: Vehicle) -> ParkingSpot | None:
        for floor in sorted(floors, key=lambda f: f.number):
            for spot_type in vehicle.allowed_spot_types:
                spot = floor.first_free(spot_type)
                if spot is not None:
                    return spot
        return None


class ElectricFirstAllocation(NearestFirstAllocation):
    """Same as nearest-first, but never gives an electric spot to a non-electric vehicle."""

    def choose(self, floors: list[ParkingFloor], vehicle: Vehicle) -> ParkingSpot | None:
        if SpotType.ELECTRIC in vehicle.allowed_spot_types[:1]:
            return super().choose(floors, vehicle)
        for floor in sorted(floors, key=lambda f: f.number):
            for spot_type in vehicle.allowed_spot_types:
                if spot_type is SpotType.ELECTRIC:
                    continue
                spot = floor.first_free(spot_type)
                if spot is not None:
                    return spot
        return None


# --8<-- [end:allocation]
