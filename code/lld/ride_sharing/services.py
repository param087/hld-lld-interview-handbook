"""Trip state, the dispatch mediator, and the observers that watch both."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Protocol

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock
from lld.ride_sharing.index import DriverLocationIndex
from lld.ride_sharing.models import (
    Driver,
    DriverOffer,
    DriverSnapshot,
    DriverStatus,
    Location,
    OfferStateError,
    OfferStatus,
    RideRequest,
    Trip,
    TripStateError,
    TripStatus,
    UnknownDriverError,
    UnknownTripError,
)
from lld.ride_sharing.strategies import FastestEta, MatchingStrategy


# --8<-- [start:observers]
class TripListener(Protocol):
    """Observer: anything that wants to know when a trip moves."""

    def on_trip_event(self, trip: Trip, event: str) -> None: ...


class TripFeed:
    """The rider-facing timeline. Never polls; ``TripService`` pushes to it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, list[str]] = {}

    def on_trip_event(self, trip: Trip, event: str) -> None:
        with self._lock:
            self._events.setdefault(trip.id, []).append(event)

    def timeline(self, trip_id: str) -> list[str]:
        with self._lock:
            return list(self._events.get(trip_id, ()))


class EarningsBoard:
    """A second observer, to prove the first one is not special: money per driver."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._earnings: dict[str, Money] = {}

    def on_trip_event(self, trip: Trip, event: str) -> None:
        if event != "completed" or trip.driver_id is None or trip.fare is None:
            return
        with self._lock:
            current = self._earnings.get(trip.driver_id, Money(0))
            self._earnings[trip.driver_id] = current + trip.fare.total

    def earnings(self, driver_id: str) -> Money:
        with self._lock:
            return self._earnings.get(driver_id, Money(0))


# --8<-- [end:observers]


# --8<-- [start:trips]
class TripService:
    """Owns the trip registry and every status change. One lock over both.

    ``transition`` is a check-and-flip against ``TRIP_TRANSITIONS``; listeners are
    notified *outside* the lock so a slow observer cannot stall dispatch.
    """

    def __init__(self, clock: Clock | None = None, ids: IdGenerator | None = None) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("T")
        self._trips: dict[str, Trip] = {}
        self._listeners: list[TripListener] = []
        self._lock = threading.Lock()

    def subscribe(self, listener: TripListener) -> None:
        self._listeners.append(listener)

    def open(self, request: RideRequest) -> Trip:
        trip = Trip(id=self._ids.next_id(), request=request)
        with self._lock:
            self._trips[trip.id] = trip
        self._notify(trip, "requested")
        return trip

    def trip(self, trip_id: str) -> Trip:
        with self._lock:
            try:
                return self._trips[trip_id]
            except KeyError:
                raise UnknownTripError(f"unknown trip {trip_id}") from None

    def history(self, rider_id: str) -> list[Trip]:
        with self._lock:
            return [t for t in self._trips.values() if t.request.rider_id == rider_id]

    def transition(self, trip_id: str, target: TripStatus, event: str | None = None) -> Trip:
        with self._lock:
            trip = self._trips.get(trip_id)
            if trip is None:
                raise UnknownTripError(f"unknown trip {trip_id}")
            if not trip.can_move_to(target):
                raise TripStateError(f"trip {trip_id} cannot move {trip.status} to {target}")
            trip.status = target
        self._notify(trip, event or str(target))
        return trip

    def assign_driver(self, trip_id: str, driver_id: str) -> Trip:
        """REQUESTED to MATCHED, atomically. A cancelled trip refuses the driver."""
        with self._lock:
            trip = self._trips.get(trip_id)
            if trip is None:
                raise UnknownTripError(f"unknown trip {trip_id}")
            if not trip.can_move_to(TripStatus.MATCHED):
                raise TripStateError(f"trip {trip_id} is {trip.status}, cannot be matched")
            trip.status = TripStatus.MATCHED
            trip.driver_id = driver_id
            trip.matched_at = self._clock.now()
        self._notify(trip, "matched")
        return trip

    def _notify(self, trip: Trip, event: str) -> None:
        for listener in self._listeners:  # outside the lock, on purpose
            listener.on_trip_event(trip, event)


# --8<-- [end:trips]


# --8<-- [start:matching]
class MatchingService:
    """Mediator between riders, drivers and the location index.

    Nothing else in the system holds both a ``Driver`` and a ``Trip``. Riders ask
    it for a driver, drivers report position and answer offers to it, and it is
    the only object that decides who is told about whom.

    One lock guards driver statuses, the offer registry and the per-trip
    shortlist. The *index* has its own striped locks and is queried without this
    lock held: the shortlist it produces is a hint, re-validated when the lease
    is taken -- the same optimistic pattern a parking lot uses to claim a spot.
    """

    OFFER_TIMEOUT_SECONDS = 15.0
    SEARCH_RADII_KM = (1.5, 3.0, 5.0)
    SHORTLIST_SIZE = 5

    def __init__(
        self,
        drivers: Iterable[Driver],
        index: DriverLocationIndex | None = None,
        strategy: MatchingStrategy | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        offer_timeout: float = OFFER_TIMEOUT_SECONDS,
    ) -> None:
        self._drivers = {d.id: d for d in drivers}
        self.index = index or DriverLocationIndex()
        self._strategy = strategy or FastestEta()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("OF")
        self._timeout = offer_timeout
        self._offers: dict[str, DriverOffer] = {}
        self._shortlists: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def driver(self, driver_id: str) -> Driver:
        try:
            return self._drivers[driver_id]
        except KeyError:
            raise UnknownDriverError(f"no driver {driver_id}") from None

    def go_online(self, driver_id: str, location: Location | None = None) -> Driver:
        with self._lock:
            driver = self.driver(driver_id)
            driver.location = location or driver.location
            driver.status = DriverStatus.AVAILABLE
        self.index.update(driver_id, driver.location)
        return driver

    def go_offline(self, driver_id: str) -> Driver:
        self.index.remove(driver_id)
        with self._lock:
            driver = self.driver(driver_id)
            driver.status = DriverStatus.OFFLINE
            return driver

    def ping(self, driver_id: str, location: Location) -> None:
        """The hot path: thousands per second, and it never touches the dispatch lock."""
        self.index.update(driver_id, location)

    def shortlist(self, request: RideRequest) -> list[str]:
        """Expanding-radius index read plus a ranking. No dispatch lock is held."""
        for radius in self.SEARCH_RADII_KM:
            snapshots = self._snapshots(self.index.nearby(request.pickup, radius), request)
            if snapshots:
                ranked = self._strategy.rank(request, snapshots)
                return [s.driver_id for s in ranked[: self.SHORTLIST_SIZE]]
        return []

    def offer_next(self, trip_id: str, request: RideRequest) -> DriverOffer | None:
        """Lease the next candidate. Idempotent while an offer for the trip is live."""
        now = self._clock.now()
        with self._lock:
            live = [o for o in self._offers.values() if o.trip_id == trip_id and o.is_live(now)]
            if live:
                return live[0]
            queue = self._shortlists.get(trip_id)
        if queue is None:
            queue = self._shortlists.setdefault(trip_id, self.shortlist(request))
        with self._lock:
            while queue:
                driver = self._drivers.get(queue.pop(0))
                if driver is None or not driver.is_available():
                    continue  # the hint went stale between the index read and here
                offer = DriverOffer(self._ids.next_id(), trip_id, driver.id, now, now + self._timeout)
                driver.status = DriverStatus.OFFERED
                driver.current_trip_id = trip_id
                self._offers[offer.id] = offer
                return offer
            return None

    def accept(self, offer_id: str, driver_id: str) -> DriverOffer:
        """Claim the lease. Exactly one caller moves an offer out of PENDING."""
        with self._lock:
            offer = self._offers.get(offer_id)
            if offer is None or offer.driver_id != driver_id:
                raise OfferStateError(f"offer {offer_id} is not addressed to {driver_id}")
            if not offer.is_live(self._clock.now()):
                raise OfferStateError(f"offer {offer_id} is {offer.status} or expired")
            offer.status = OfferStatus.ACCEPTED
            self.driver(driver_id).status = DriverStatus.ON_TRIP
            return offer

    def decline(self, offer_id: str, driver_id: str) -> DriverOffer:
        with self._lock:
            offer = self._offers.get(offer_id)
            if offer is None or offer.driver_id != driver_id:
                raise OfferStateError(f"offer {offer_id} is not addressed to {driver_id}")
            if offer.status is not OfferStatus.PENDING:
                raise OfferStateError(f"offer {offer_id} is already {offer.status}")
            self._retire(offer, OfferStatus.DECLINED)
            return offer

    def sweep(self) -> list[DriverOffer]:
        """Expire every lease past its deadline and free those drivers."""
        now = self._clock.now()
        with self._lock:
            stale = [o for o in self._offers.values() if o.status is OfferStatus.PENDING and now >= o.expires_at]
            for offer in stale:
                self._retire(offer, OfferStatus.EXPIRED)
            return stale

    def release(self, offer: DriverOffer) -> None:
        """Undo a claim because the trip moved under the driver."""
        with self._lock:
            self._retire(offer, OfferStatus.VOIDED)

    def void_trip(self, trip_id: str) -> None:
        with self._lock:
            for offer in self._offers.values():
                if offer.trip_id == trip_id and offer.status in (OfferStatus.PENDING, OfferStatus.ACCEPTED):
                    self._retire(offer, OfferStatus.VOIDED)
            self._shortlists.pop(trip_id, None)

    def finish(self, driver_id: str) -> Driver:
        with self._lock:
            driver = self.driver(driver_id)
            driver.status = DriverStatus.AVAILABLE
            driver.current_trip_id = None
            driver.trips_today += 1
        self.index.update(driver_id, driver.location)
        return driver

    def offers_for(self, trip_id: str) -> list[DriverOffer]:
        with self._lock:
            return [o for o in self._offers.values() if o.trip_id == trip_id]

    def _snapshots(self, driver_ids: Iterable[str], request: RideRequest) -> list[DriverSnapshot]:
        """Freeze the eligible drivers. Availability here is a hint, not a promise."""
        out: list[DriverSnapshot] = []
        for driver_id in driver_ids:
            driver = self._drivers.get(driver_id)
            position = self.index.position(driver_id)
            if driver is None or position is None or not driver.is_available():
                continue
            if not driver.vehicle.serves(request.ride_type):
                continue
            out.append(DriverSnapshot(driver.id, position, driver.rating, driver.trips_today))
        return out

    def _retire(self, offer: DriverOffer, status: OfferStatus) -> None:
        """Caller holds the lock. The single place a leased driver is freed."""
        offer.status = status
        driver = self._drivers.get(offer.driver_id)
        if driver is not None and driver.status is not DriverStatus.OFFLINE:
            driver.status = DriverStatus.AVAILABLE
            driver.current_trip_id = None


# --8<-- [end:matching]
