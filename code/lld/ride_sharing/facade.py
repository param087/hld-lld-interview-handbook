"""The one object an API layer holds: request a ride, drive it, pay for it."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock
from lld.ride_sharing.index import DriverLocationIndex
from lld.ride_sharing.models import (
    Driver,
    DriverOffer,
    Fare,
    Location,
    Payment,
    Rating,
    Rider,
    RideRequest,
    RideType,
    Trip,
    TripStateError,
    TripStatus,
)
from lld.ride_sharing.services import EarningsBoard, MatchingService, TripFeed, TripService
from lld.ride_sharing.strategies import (
    CancellationPolicy,
    FareCalculator,
    MatchingStrategy,
    SurgeProvider,
)


# --8<-- [start:facade]
class RideHailingService:
    """Facade over trips, matching and money. It sequences; it never computes.

    Every cross-service step is *claim, act, revert*: ``driver_accepts`` claims
    the lease inside ``MatchingService``, then matches the trip inside
    ``TripService``, and releases the lease if the trip has moved on. No method
    ever holds two service locks, so there is no lock order to get wrong.
    """

    def __init__(
        self,
        riders: Iterable[Rider],
        drivers: Iterable[Driver],
        index: DriverLocationIndex | None = None,
        strategy: MatchingStrategy | None = None,
        surge: SurgeProvider | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        request_ids: IdGenerator | None = None,
        offer_timeout: float = MatchingService.OFFER_TIMEOUT_SECONDS,
    ) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("T")
        self._request_ids = request_ids or SequentialIdGenerator("RQ")
        self._riders = {r.id: r for r in riders}
        self.trips = TripService(clock=self._clock, ids=self._ids)
        self.matching = MatchingService(
            drivers, index=index, strategy=strategy, clock=self._clock, offer_timeout=offer_timeout
        )
        self.fares = FareCalculator(surge)
        self.cancellation = CancellationPolicy()
        self.feed = TripFeed()
        self.earnings = EarningsBoard()
        self.trips.subscribe(self.feed)
        self.trips.subscribe(self.earnings)
        self._payments: dict[str, Payment] = {}
        self._ratings: list[Rating] = []
        self._lock = threading.Lock()

    # -- rider ------------------------------------------------------------------
    def estimate(self, pickup: Location, dropoff: Location, ride_type: RideType) -> Fare:
        request = RideRequest("estimate", "-", pickup, dropoff, RideType(ride_type), self._clock.now())
        return self.fares.estimate(request)

    def request_ride(self, rider_id: str, pickup: Location, dropoff: Location, ride_type: RideType) -> Trip:
        """Open a trip, then offer it to the best candidate the index knows about."""
        if rider_id not in self._riders:
            raise TripStateError(f"unknown rider {rider_id}")
        request = RideRequest(
            id=self._request_ids.next_id(),
            rider_id=rider_id,
            pickup=pickup,
            dropoff=dropoff,
            ride_type=RideType(ride_type),
            requested_at=self._clock.now(),
        )
        trip = self.trips.open(request)
        self._offer_next(trip)
        return trip

    def cancel_ride(self, trip_id: str) -> Trip:
        """Flip the trip first, so a driver accepting at the same instant loses."""
        trip = self.trips.trip(trip_id)
        fee = self.cancellation.fee(trip, self._clock.now())
        cancelled = self.trips.cancel(trip_id, fee)
        self.matching.void_trip(trip_id)
        return cancelled

    def history(self, rider_id: str) -> list[Trip]:
        return self.trips.history(rider_id)

    # -- driver -----------------------------------------------------------------
    def driver_accepts(self, offer_id: str, driver_id: str) -> Trip:
        offer = self.matching.accept(offer_id, driver_id)  # claim the lease
        try:
            return self.trips.assign_driver(offer.trip_id, driver_id)
        except TripStateError:
            self.matching.release(offer)  # the rider cancelled underneath
            raise

    def driver_declines(self, offer_id: str, driver_id: str) -> DriverOffer | None:
        offer = self.matching.decline(offer_id, driver_id)
        return self._offer_next(self.trips.trip(offer.trip_id))

    def sweep_offers(self) -> list[DriverOffer]:
        """Expire stale leases, then push each waiting trip down its shortlist."""
        expired = self.matching.sweep()
        for offer in expired:
            self._offer_next(self.trips.trip(offer.trip_id))
        return expired

    def driver_arrived(self, trip_id: str) -> Trip:
        return self.trips.transition(trip_id, TripStatus.ARRIVED, "arrived")

    def start_trip(self, trip_id: str) -> Trip:
        return self.trips.start(trip_id)

    def end_trip(self, trip_id: str, distance_km: float | None = None) -> Trip:
        """Stop the meter, price the fare outside every lock, then commit it."""
        trip = self.trips.trip(trip_id)
        metered = distance_km if distance_km is not None else trip.request.straight_line_km
        trip = self.trips.finish_metering(trip_id, metered)
        fare = self.fares.quote(trip.request.ride_type, trip.distance_km, trip.minutes(), trip.request.pickup)
        completed = self.trips.complete(trip_id, fare)
        if completed.driver_id:
            self.matching.finish(completed.driver_id)
        return completed

    # -- money and reputation ---------------------------------------------------
    def pay(self, trip_id: str) -> Payment:
        trip = self.trips.trip(trip_id)
        amount = trip.fare.total if trip.fare else (trip.cancellation_fee or Money(0))
        if trip.status not in (TripStatus.COMPLETED, TripStatus.CANCELLED):
            raise TripStateError(f"trip {trip_id} is {trip.status}, nothing to charge")
        payment = Payment(f"PAY-{trip_id}", trip_id, amount, captured=True)
        with self._lock:
            self._payments[trip_id] = payment
        return payment

    def rate_driver(self, trip_id: str, stars: int, comment: str = "") -> Rating:
        trip = self.trips.trip(trip_id)
        if trip.status is not TripStatus.COMPLETED or trip.driver_id is None:
            raise TripStateError(f"trip {trip_id} was not completed")
        rating = Rating(trip_id, trip.driver_id, stars, comment)
        driver = self.matching.driver(trip.driver_id)
        with self._lock:
            self._ratings.append(rating)
            total = driver.rating * driver.ratings_count + stars
            driver.ratings_count += 1
            driver.rating = round(total / driver.ratings_count, 2)
        return rating

    def _offer_next(self, trip: Trip) -> DriverOffer | None:
        """Offer to the next candidate, or declare the cascade exhausted."""
        if trip.status is not TripStatus.REQUESTED:
            return None
        offer = self.matching.offer_next(trip.id, trip.request)
        if offer is None:
            self.trips.transition(trip.id, TripStatus.NO_DRIVER, "no_driver")
        return offer


# --8<-- [end:facade]
