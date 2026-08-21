"""Ride-hailing dispatch: a geohash-bucketed driver index, nearest-K with radius expansion, leases.

The crux of the Uber design in one module, built on the geohash index from ``hld.geohash`` so the
dispatcher and the proximity service share one partitioning scheme:

* ``go_online`` and ``update_location`` keep every *dispatchable* driver in a geohash-bucketed
  index, so a match reads a handful of cells instead of scanning a city.
* ``request_ride`` searches nearest-K in a small radius and doubles it until candidates appear or
  the cap is reached, then offers the trip to the closest driver.
* An offer takes a **lease** on that driver: the driver leaves the available index for as long as
  the offer stands, which is what makes double dispatch impossible. A declined or expired lease
  returns the driver to the index and the trip is re-offered to the next candidate.
* ``TripState`` is the lifecycle the interviewer asks you to draw, with the retry loop
  ``OFFERED -> REQUESTED -> OFFERED`` that a naive design forgets.
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from common import (
    Clock,
    ConflictError,
    IdGenerator,
    InvalidStateError,
    NotFoundError,
    SequentialIdGenerator,
    SystemClock,
    ValidationError,
)
from hld.geohash import GeoIndex, Hit, cell_size_km, encode, precision_for_radius_km


# --8<-- [start:models]
class TripState(StrEnum):
    """Every state an interviewer expects on the whiteboard, and no more."""

    REQUESTED = "requested"
    OFFERED = "offered"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNFULFILLED = "unfulfilled"


_TRANSITIONS = MappingProxyType({
    TripState.REQUESTED: frozenset({TripState.OFFERED, TripState.UNFULFILLED, TripState.CANCELLED}),
    TripState.OFFERED: frozenset({TripState.ASSIGNED, TripState.REQUESTED, TripState.CANCELLED}),
    TripState.ASSIGNED: frozenset({TripState.IN_PROGRESS, TripState.CANCELLED}),
    TripState.IN_PROGRESS: frozenset({TripState.COMPLETED}),
    TripState.COMPLETED: frozenset(),
    TripState.CANCELLED: frozenset(),
    TripState.UNFULFILLED: frozenset(),
})


@dataclass(frozen=True, slots=True)
class Offer:
    """A dispatch offer. It holds a lease on ``driver_id`` until ``expires_at``."""

    trip_id: str
    driver_id: str
    distance_km: float
    expires_at: float


@dataclass(slots=True)
class Trip:
    id: str
    rider_id: str
    pickup_lat: float
    pickup_lon: float
    created_at: float
    state: TripState = TripState.REQUESTED
    driver_id: str | None = None
    offers: int = 0
    matched_radius_km: float = 0.0
    declined: set[str] = field(default_factory=set)

    def to(self, state: TripState) -> None:
        if state not in _TRANSITIONS[self.state]:
            raise InvalidStateError(f"trip {self.id}: {self.state} -> {state} is not a legal transition")
        self.state = state


# --8<-- [end:models]


# --8<-- [start:service]
class DispatchService:
    """In-memory stand-in for: the location index (Redis/H3 shards), the lease store and the trip DB.

    ``_index`` holds only dispatchable drivers, so a candidate search can never return a driver who
    is offline, already holding an offer or already driving. ``_lock`` guards every dictionary
    below; it is an ``RLock`` because a declined offer re-enters dispatch while the lock is held.
    """

    def __init__(
        self,
        offer_timeout_s: float = 15.0,
        initial_radius_km: float = 1.0,
        max_radius_km: float = 4.0,
        candidates: int = 3,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        if not 0 < initial_radius_km <= max_radius_km:
            raise ValidationError("need 0 < initial_radius_km <= max_radius_km")
        if offer_timeout_s <= 0 or candidates <= 0:
            raise ValidationError("offer_timeout_s and candidates must be positive")
        self._offer_timeout = offer_timeout_s
        self._initial_radius = initial_radius_km
        self._max_radius = max_radius_km
        self._candidates = candidates
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("trip")
        self._index = GeoIndex(precision_for_radius_km(max_radius_km))
        self._locations: dict[str, tuple[float, float, str]] = {}  # driver -> lat, lon, cell
        self._indexed: set[str] = set()  # drivers currently in _index
        self._leases: dict[str, Offer] = {}  # driver -> outstanding offer
        self._on_trip: dict[str, str] = {}  # driver -> accepted trip
        self._trips: dict[str, Trip] = {}
        self._demand: Counter[str] = Counter()  # cell -> ride requests seen
        self._lock = threading.RLock()

    @property
    def precision(self) -> int:
        return self._index.precision

    # -- supply: location pings ------------------------------------------------------------
    def go_online(self, driver_id: str, lat: float, lon: float) -> None:
        """Driver becomes dispatchable at ``(lat, lon)``."""
        with self._lock:
            self._place(driver_id, lat, lon)

    def update_location(self, driver_id: str, lat: float, lon: float) -> None:
        """A location ping. One every 4 s per driver makes this the hottest write in the system."""
        with self._lock:
            if driver_id not in self._locations:
                raise NotFoundError(f"driver {driver_id!r} is not online")
            self._place(driver_id, lat, lon)

    def go_offline(self, driver_id: str) -> None:
        """Drop the driver; an outstanding offer is treated as a decline and re-dispatched."""
        with self._lock:
            if (offer := self._leases.get(driver_id)) is not None:
                self.decline_offer(driver_id, offer.trip_id)
            self._locations.pop(driver_id, None)
            self._sync_index(driver_id)

    def _place(self, driver_id: str, lat: float, lon: float) -> None:
        """Store the ping and re-index; ``encode`` validates the coordinates. Caller holds the lock."""
        self._locations[driver_id] = (lat, lon, encode(lat, lon, self._index.precision))
        self._sync_index(driver_id)

    def _sync_index(self, driver_id: str) -> None:
        """Make index membership match dispatchability, in O(1). Caller holds the lock."""
        free = (
            driver_id in self._locations
            and driver_id not in self._leases
            and driver_id not in self._on_trip
        )
        if free:
            self._index.add(driver_id, *self._locations[driver_id][:2])  # add() also moves
            self._indexed.add(driver_id)
        elif driver_id in self._indexed:
            self._index.remove(driver_id)
            self._indexed.discard(driver_id)

    # -- matching --------------------------------------------------------------------------
    def _search(self, lat: float, lon: float, k: int, exclude: frozenset[str]) -> tuple[list[Hit], float]:
        """Nearest-K over the available index, doubling the radius until it finds candidates.

        Returns the hits and the radius that produced them: a match found only at 4 km is a supply
        problem worth logging, not just a slow request. Caller holds the lock.
        """
        radius = self._initial_radius
        while True:
            found = self._index.nearby(lat, lon, radius, limit=k + len(exclude))
            hits = [h for h in found if h.item_id not in exclude]
            if hits or radius >= self._max_radius:
                return hits[:k], radius
            radius = min(radius * 2, self._max_radius)

    def nearest_available(self, lat: float, lon: float, k: int = 3) -> list[Hit]:
        """The candidate list a dispatch would consider right now, nearest first."""
        if k <= 0:
            raise ValidationError("k must be positive")
        with self._lock:
            self.reap_expired_offers()
            return self._search(lat, lon, k, frozenset())[0]

    def request_ride(self, rider_id: str, lat: float, lon: float) -> Trip:
        """Create a trip and offer it to the nearest dispatchable driver."""
        with self._lock:
            self.reap_expired_offers()
            trip = Trip(self._ids.next_id(), rider_id, lat, lon, self._clock.now())
            self._trips[trip.id] = trip
            self._demand[encode(lat, lon, self._index.precision)] += 1
            self._offer_to_next(trip)
            return trip

    def _offer_to_next(self, trip: Trip) -> Offer | None:
        """Lease the closest untried driver, or give up. Caller holds the lock, trip is REQUESTED."""
        hits, radius = self._search(trip.pickup_lat, trip.pickup_lon, self._candidates, frozenset(trip.declined))
        if not hits:
            trip.to(TripState.UNFULFILLED)
            return None
        best = hits[0]
        offer = Offer(trip.id, best.item_id, best.distance_km, self._clock.now() + self._offer_timeout)
        self._leases[best.item_id] = offer  # the lease is what prevents double dispatch
        self._sync_index(best.item_id)  # ... because it removes the driver from the index
        trip.offers += 1
        trip.matched_radius_km = radius
        trip.to(TripState.OFFERED)
        return offer

    def offer_for(self, driver_id: str) -> Offer | None:
        with self._lock:
            return self._leases.get(driver_id)

    def outstanding_offers(self) -> list[Offer]:
        with self._lock:
            return sorted(self._leases.values(), key=lambda o: o.driver_id)

    def accept_offer(self, driver_id: str, trip_id: str) -> Trip:
        with self._lock:
            offer = self._require_lease(driver_id, trip_id)
            if self._clock.now() >= offer.expires_at:
                raise ConflictError(f"the offer on {trip_id!r} expired and was re-dispatched")
            trip = self.trip(trip_id)
            trip.to(TripState.ASSIGNED)
            trip.driver_id = driver_id
            del self._leases[driver_id]
            self._on_trip[driver_id] = trip_id
            self._sync_index(driver_id)  # still not dispatchable: now on a trip
            return trip

    def decline_offer(self, driver_id: str, trip_id: str) -> Trip:
        with self._lock:
            self._require_lease(driver_id, trip_id)
            trip = self.trip(trip_id)
            trip.declined.add(driver_id)
            del self._leases[driver_id]
            self._sync_index(driver_id)
            trip.to(TripState.REQUESTED)
            self._offer_to_next(trip)
            return trip

    def reap_expired_offers(self) -> list[str]:
        """Release timed-out leases and re-dispatch their trips; returns the drivers released.

        Production lets the lease TTL do this (a Redis key that simply disappears); an explicit
        sweep keeps the demo and the tests deterministic.
        """
        with self._lock:
            now = self._clock.now()
            expired = [o for o in self._leases.values() if o.expires_at <= now]
            for offer in expired:
                del self._leases[offer.driver_id]
                self._sync_index(offer.driver_id)
                trip = self._trips[offer.trip_id]
                if trip.state is TripState.OFFERED:
                    trip.declined.add(offer.driver_id)
                    trip.to(TripState.REQUESTED)
                    self._offer_to_next(trip)
            return [o.driver_id for o in expired]

    def _require_lease(self, driver_id: str, trip_id: str) -> Offer:
        offer = self._leases.get(driver_id)
        if offer is None or offer.trip_id != trip_id:
            raise ConflictError(f"driver {driver_id!r} holds no lease on trip {trip_id!r}")
        return offer

    # -- trip lifecycle --------------------------------------------------------------------
    def trip(self, trip_id: str) -> Trip:
        with self._lock:
            if trip_id not in self._trips:
                raise NotFoundError(f"unknown trip {trip_id!r}")
            return self._trips[trip_id]

    def start_trip(self, trip_id: str) -> Trip:
        """The rider is on board."""
        with self._lock:
            trip = self.trip(trip_id)
            trip.to(TripState.IN_PROGRESS)
            return trip

    def complete_trip(self, trip_id: str) -> Trip:
        with self._lock:
            trip = self.trip(trip_id)
            trip.to(TripState.COMPLETED)
            if trip.driver_id is not None:
                self._on_trip.pop(trip.driver_id, None)
                self._sync_index(trip.driver_id)  # dispatchable again
            return trip

    def cancel_trip(self, trip_id: str) -> Trip:
        """A rider cancel releases whatever the trip is holding: a lease or an assigned driver."""
        with self._lock:
            trip = self.trip(trip_id)
            released = trip.driver_id
            if trip.state is TripState.OFFERED:
                released = next((o.driver_id for o in self._leases.values() if o.trip_id == trip_id), None)
                if released is not None:
                    del self._leases[released]
            trip.to(TripState.CANCELLED)
            if released is not None:
                self._on_trip.pop(released, None)
                self._sync_index(released)
            return trip

    # -- pricing ---------------------------------------------------------------------------
    def cell_load(self, lat: float, lon: float) -> tuple[int, int]:
        """``(ride requests seen, dispatchable drivers)`` in the cell containing the point."""
        with self._lock:
            cell = encode(lat, lon, self._index.precision)
            supply = sum(1 for d in self._indexed if self._locations[d][2] == cell)
            return self._demand[cell], supply

    def surge_multiplier(self, lat: float, lon: float, cap: float = 3.0) -> float:
        """Requests over dispatchable drivers in one cell, rounded to 0.1 and capped.

        The point to make out loud: surge is computed from the same cell key the dispatcher
        searches, so the price a rider is quoted and the supply they can be matched with come from
        one partitioning scheme rather than two.
        """
        demand, supply = self.cell_load(lat, lon)
        if demand <= supply:
            return 1.0
        return min(cap, round(demand / max(supply, 1), 1))


# --8<-- [end:service]


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=1_700_000_000)
    dispatch = DispatchService(offer_timeout_s=15.0, max_radius_km=4.0, clock=clock)
    width, height = cell_size_km(dispatch.precision, lat=37.8)
    print(f"driver index: geohash precision {dispatch.precision} ({width:.1f} km x {height:.1f} km cells)")

    drivers = {
        "d_ann": (37.7879, -122.4074),  # Union Square
        "d_bob": (37.7844, -122.4078),  # Powell St station
        "d_cy": (37.7955, -122.3937),  # Ferry Building
        "d_dee": (37.8024, -122.4058),  # Coit Tower
        "d_eve": (37.7946, -122.2781),  # Oakland
    }
    for name, (lat, lon) in drivers.items():
        dispatch.go_online(name, lat, lon)
    pickup = (37.7880, -122.4075)
    candidates = dispatch.nearest_available(*pickup, k=3)
    ranked = " | ".join(f"{h.item_id} {h.distance_km:.2f} km" for h in candidates)
    print(f"{len(drivers)} drivers online; inside the 1 km start radius of the pickup: {ranked}")

    trip = dispatch.request_ride("r_1", *pickup)
    offer = dispatch.outstanding_offers()[0]
    print(f"{trip.id} {trip.state} to {offer.driver_id} ({offer.distance_km:.2f} km), lease for 15 s")
    other = [h.item_id for h in dispatch.nearest_available(*pickup, k=3)]
    print(f"  a rider requesting the same corner now sees {other}: a leased driver is never offered twice")

    clock.advance(16)
    print(f"offer timed out for {dispatch.reap_expired_offers()}: {trip.id} is {trip.state} again")
    winner = dispatch.outstanding_offers()[0].driver_id
    dispatch.accept_offer(winner, trip.id)
    print(f"{winner} accepted offer {trip.offers}: {trip.id} is {trip.state} within {trip.matched_radius_km:g} km")

    thin_trip = dispatch.request_ride("r_2", 37.7749, -122.4400)  # the Panhandle: thin supply
    thin_offer = dispatch.outstanding_offers()[0]
    print(
        f"{thin_trip.id} in thin supply: {thin_offer.driver_id} at {thin_offer.distance_km:.2f} km, "
        f"found after expanding 1 km -> {thin_trip.matched_radius_km:g} km"
    )
    far_trip = dispatch.request_ride("r_3", 37.4419, -122.1430)  # Palo Alto, 50 km out
    print(f"{far_trip.id} 50 km away: {far_trip.state} after {far_trip.offers} offers, nobody inside 4 km")
    for _ in range(2):
        dispatch.request_ride("r_burst", *pickup)
    demand, supply = dispatch.cell_load(*pickup)
    print(f"pickup cell: {demand} requests against {supply} dispatchable drivers -> surge x{dispatch.surge_multiplier(*pickup)}")
    dispatch.start_trip(trip.id)
    dispatch.complete_trip(trip.id)
    back = [h.item_id for h in dispatch.nearest_available(*pickup, k=5)]
    print(f"{trip.id} is {trip.state}; dispatchable near the pickup again: {back}")


if __name__ == "__main__":
    main()
