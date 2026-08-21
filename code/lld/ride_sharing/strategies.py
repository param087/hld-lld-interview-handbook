"""Pluggable policies: how to rank drivers, how surge is set, how a fare is built."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from common import Money
from lld.ride_sharing.index import Cell, DriverLocationIndex
from lld.ride_sharing.models import (
    CHARGEABLE_CANCEL_FROM,
    DriverSnapshot,
    Fare,
    Location,
    RideRequest,
    RideType,
    RideTypeFactory,
    Trip,
)

AVERAGE_CITY_SPEED_KMH = 24.0


# --8<-- [start:matching]
class MatchingStrategy(Protocol):
    """Ranks an already-filtered shortlist of drivers, best first.

    It receives immutable ``DriverSnapshot`` values, so a strategy cannot mutate
    dispatch state or take a lock. Swapping the ranking rule therefore cannot
    introduce a race -- that is the whole reason the snapshot type exists.
    """

    def rank(self, request: RideRequest, candidates: Sequence[DriverSnapshot]) -> list[DriverSnapshot]: ...


class NearestDriver:
    """Straight-line distance to the pickup. The answer everyone gives first."""

    def rank(self, request: RideRequest, candidates: Sequence[DriverSnapshot]) -> list[DriverSnapshot]:
        return sorted(candidates, key=lambda d: (d.pickup_km(request.pickup), d.driver_id))


class FastestEta:
    """Distance turned into minutes at an assumed city speed, rounded to 0.1 min.

    Rounding matters: without it, two drivers 40 metres apart are ordered by
    floating-point noise, and the same request ranks differently on every run.
    """

    def __init__(self, speed_kmh: float = AVERAGE_CITY_SPEED_KMH) -> None:
        self._speed = speed_kmh

    def eta_minutes(self, snapshot: DriverSnapshot, pickup: Location) -> float:
        return round(snapshot.pickup_km(pickup) / self._speed * 60.0, 1)

    def rank(self, request: RideRequest, candidates: Sequence[DriverSnapshot]) -> list[DriverSnapshot]:
        return sorted(
            candidates,
            key=lambda d: (self.eta_minutes(d, request.pickup), -d.rating, d.driver_id),
        )


class HighestRatedNearby:
    """Best rated first, distance only as a tie-break. Used for premium classes."""

    def rank(self, request: RideRequest, candidates: Sequence[DriverSnapshot]) -> list[DriverSnapshot]:
        return sorted(candidates, key=lambda d: (-d.rating, d.pickup_km(request.pickup), d.driver_id))


class FairRotation:
    """Fewest trips today first. Keeps a shift's earnings even across drivers."""

    def rank(self, request: RideRequest, candidates: Sequence[DriverSnapshot]) -> list[DriverSnapshot]:
        return sorted(candidates, key=lambda d: (d.trips_today, d.pickup_km(request.pickup), d.driver_id))


# --8<-- [end:matching]


# --8<-- [start:fare]
class SurgeProvider(Protocol):
    """The multiplier applied to a fare at this pickup point, right now."""

    def multiplier(self, pickup: Location) -> float: ...


class NoSurge:
    """Null Object: the default. Pricing never branches on ``surge is None``."""

    def multiplier(self, pickup: Location) -> float:
        return 1.0


class FlatSurge:
    """One multiplier over the whole city -- a bank holiday, a storm."""

    def __init__(self, multiplier: float = 1.0) -> None:
        self._multiplier = multiplier

    def multiplier(self, pickup: Location) -> float:
        return self._multiplier


class ZoneSurge:
    """Per-cell multipliers, read off the same grid dispatch already uses.

    Reusing the index geometry is the point: a surge zone is a set of cells, so
    the pricing team and the dispatch team argue about the same coordinates.
    """

    def __init__(self, index: DriverLocationIndex, zones: dict[Cell, float], default: float = 1.0) -> None:
        self._index = index
        self._zones = dict(zones)
        self._default = default

    def multiplier(self, pickup: Location) -> float:
        return self._zones.get(self._index.cell_of(pickup), self._default)


class FareCalculator:
    """base + per km + per minute, multiplied by surge, floored at the minimum."""

    def __init__(self, surge: SurgeProvider | None = None) -> None:
        self._surge = surge or NoSurge()

    def quote(self, ride_type: RideType, distance_km: float, minutes: float, pickup: Location) -> Fare:
        spec = RideTypeFactory.spec(ride_type)
        multiplier = self._surge.multiplier(pickup)
        distance = spec.per_km * Decimal(str(round(max(distance_km, 0.0), 2)))
        time = spec.per_minute * Decimal(str(round(max(minutes, 0.0), 2)))
        raw = (spec.base + distance + time) * Decimal(str(multiplier))
        return Fare(spec.base, distance, time, multiplier, max(raw, spec.minimum))

    def estimate(self, request: RideRequest, speed_kmh: float = AVERAGE_CITY_SPEED_KMH) -> Fare:
        """The number shown before the rider confirms: straight line at city speed."""
        distance = request.straight_line_km
        return self.quote(request.ride_type, distance, distance / speed_kmh * 60.0, request.pickup)


class CancellationPolicy:
    """Free before a driver commits, and free for a short grace window after."""

    FEE = Money.of("3.00")
    GRACE_SECONDS = 120.0

    def fee(self, trip: Trip, now: float) -> Money:
        if trip.status not in CHARGEABLE_CANCEL_FROM or trip.matched_at is None:
            return Money(0)
        if now - trip.matched_at <= self.GRACE_SECONDS:
            return Money(0)
        return self.FEE


# --8<-- [end:fare]
