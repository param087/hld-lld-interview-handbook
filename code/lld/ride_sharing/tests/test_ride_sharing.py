import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, Money, SequentialIdGenerator
from lld.ride_sharing.facade import RideHailingService
from lld.ride_sharing.index import DriverLocationIndex
from lld.ride_sharing.models import (
    Driver,
    DriverSnapshot,
    DriverStatus,
    Location,
    OfferStateError,
    OfferStatus,
    Rider,
    RideRequest,
    RideType,
    TripStateError,
    TripStatus,
    Vehicle,
)
from lld.ride_sharing.strategies import (
    FairRotation,
    FastestEta,
    HighestRatedNearby,
    MatchingStrategy,
    NearestDriver,
    ZoneSurge,
)

START_EPOCH = 1_772_020_800.0  # 2026-02-25T12:00Z
PICKUP = Location(12.9716, 77.5946)
DROPOFF = Location(13.0100, 77.6200)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START_EPOCH)


def make_driver(index: int, ride_types: set[RideType] | None = None, rating: float = 4.5) -> Driver:
    types = frozenset(ride_types or {RideType.ECONOMY})
    # Fan the drivers out along a line so every distance is distinct and stable.
    location = Location(PICKUP.lat + 0.0015 * index, PICKUP.lon + 0.0015 * index)
    return Driver(f"d{index}", f"driver {index}", Vehicle(f"KA{index}", "hatch", 4, types), location, rating=rating)


def build(clock: FakeClock, drivers: list[Driver], surge: float | None = None, timeout: float = 15.0) -> RideHailingService:
    index = DriverLocationIndex(cell_size_km=1.0, reference_lat=12.97, stripes=8)
    zones = {index.cell_of(PICKUP): surge} if surge else {}
    service = RideHailingService(
        [Rider("r1", "Rider One")], drivers, index=index, strategy=FastestEta(),
        surge=ZoneSurge(index, zones), clock=clock, ids=SequentialIdGenerator("T"), offer_timeout=timeout,
    )
    for driver in drivers:
        service.matching.go_online(driver.id)
    return service


def test_full_trip_prices_base_distance_time_and_surge(clock: FakeClock) -> None:
    service = build(clock, [make_driver(1)], surge=1.5)
    trip = service.request_ride("r1", PICKUP, DROPOFF, RideType.ECONOMY)
    offer = service.matching.offers_for(trip.id)[0]
    service.driver_accepts(offer.id, offer.driver_id)
    service.driver_arrived(trip.id)
    service.start_trip(trip.id)
    clock.advance(20 * 60)
    completed = service.end_trip(trip.id, distance_km=6.0)
    # (2.00 base + 6.0 x 0.90 + 20 x 0.20) x 1.5 = (2.00 + 5.40 + 4.00) x 1.5
    assert completed.fare is not None and completed.fare.total == Money.of("17.10")
    assert service.pay(trip.id).amount == Money.of("17.10")
    assert service.matching.driver("d1").status is DriverStatus.AVAILABLE
    assert service.feed.timeline(trip.id) == ["requested", "matched", "arrived", "in_progress", "completed"]
    assert service.earnings.earnings("d1") == Money.of("17.10")


def test_grid_index_moves_a_driver_between_cells_and_filters_exactly() -> None:
    index = DriverLocationIndex(cell_size_km=1.0, reference_lat=12.97, stripes=4)
    home = index.cell_of(PICKUP)
    index.update("d1", PICKUP)
    assert index.cell_population(home) == 1 and index.nearby(PICKUP, 1.0) == ["d1"]

    far = Location(PICKUP.lat + 0.05, PICKUP.lon)  # ~5.5 km north
    index.update("d1", far)
    assert index.cell_population(home) == 0  # the old cell is emptied, not just shadowed
    assert index.nearby(PICKUP, 1.0) == [] and index.nearby(PICKUP, 6.0) == ["d1"]
    index.remove("d1")
    assert index.size() == 0 and index.nearby(PICKUP, 50.0) == []


# --8<-- [start:index_race]
def test_concurrent_pings_never_leave_a_driver_in_two_cells() -> None:
    index = DriverLocationIndex(cell_size_km=1.0, reference_lat=12.97, stripes=8)
    ids = [f"d{i}" for i in range(12)]
    rng = random.Random(42)
    # Two threads ping the *same* driver, which is the interleaving that corrupts
    # a naive index: one thread adds to the new cell while the other is removing.
    pings = [(driver_id, Location(PICKUP.lat + rng.uniform(-0.02, 0.02), PICKUP.lon + rng.uniform(-0.02, 0.02)))
             for driver_id in ids for _ in range(40)]
    rng.shuffle(pings)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda p: index.update(*p), pings))
    for driver_id in ids:  # settle every driver on a known point
        index.update(driver_id, PICKUP)

    found = index.nearby(PICKUP, 5.0)
    assert sorted(found) == sorted(ids)  # equal length means no driver was duplicated or lost
    assert index.cell_population(index.cell_of(PICKUP)) == len(ids)


def test_concurrent_requests_never_offer_one_driver_two_trips(clock: FakeClock) -> None:
    service = build(clock, [make_driver(i) for i in range(1, 4)])

    def request(i: int) -> str | None:
        trip = service.request_ride("r1", PICKUP, DROPOFF, RideType.ECONOMY)
        offers = service.matching.offers_for(trip.id)
        return offers[0].driver_id if offers else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        leased = [d for d in pool.map(request, range(8)) if d is not None]

    assert len(leased) == 3 and len(set(leased)) == 3  # three drivers, three leases
    assert all(service.matching.driver(d).status is DriverStatus.OFFERED for d in leased)
    no_driver = [t for t in service.history("r1") if t.status is TripStatus.NO_DRIVER]
    assert len(no_driver) == 5  # the other five riders are told nobody is free


# --8<-- [end:index_race]


def test_only_one_thread_can_accept_an_offer(clock: FakeClock) -> None:
    service = build(clock, [make_driver(1)])
    trip = service.request_ride("r1", PICKUP, DROPOFF, RideType.ECONOMY)
    offer = service.matching.offers_for(trip.id)[0]

    def accept(_: int) -> bool:
        try:
            service.driver_accepts(offer.id, "d1")
        except (OfferStateError, TripStateError):
            return False
        return True

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(accept, range(12)))
    assert results.count(True) == 1
    assert trip.status is TripStatus.MATCHED and trip.driver_id == "d1"


def test_cancel_during_the_offer_frees_the_driver(clock: FakeClock) -> None:
    service = build(clock, [make_driver(1)])
    trip = service.request_ride("r1", PICKUP, DROPOFF, RideType.ECONOMY)
    offer = service.matching.offers_for(trip.id)[0]
    assert service.cancel_ride(trip.id).cancellation_fee == Money(0)  # nobody had committed
    assert service.matching.driver("d1").status is DriverStatus.AVAILABLE
    assert service.matching.offers_for(trip.id)[0].status is OfferStatus.VOIDED
    with pytest.raises(OfferStateError):
        service.driver_accepts(offer.id, "d1")


def test_timeout_walks_the_shortlist_and_then_gives_up(clock: FakeClock) -> None:
    service = build(clock, [make_driver(1), make_driver(2)], timeout=15.0)
    trip = service.request_ride("r1", PICKUP, DROPOFF, RideType.ECONOMY)
    first = service.matching.offers_for(trip.id)[0]
    assert first.driver_id == "d1"  # nearest by ETA

    clock.advance(16)
    assert [o.status for o in service.sweep_offers()] == [OfferStatus.EXPIRED]
    second = service.matching.offers_for(trip.id)[-1]
    assert second.driver_id == "d2" and service.matching.driver("d1").status is DriverStatus.AVAILABLE

    clock.advance(16)
    service.sweep_offers()
    assert trip.status is TripStatus.NO_DRIVER  # the cascade is exhausted, not retried forever


@pytest.mark.parametrize(
    ("target", "legal"),
    [
        (TripStatus.MATCHED, True),
        (TripStatus.CANCELLED, True),
        (TripStatus.NO_DRIVER, True),
        (TripStatus.ARRIVED, False),
        (TripStatus.IN_PROGRESS, False),
        (TripStatus.COMPLETED, False),
    ],
)
def test_the_transition_table_is_the_only_gate(clock: FakeClock, target: TripStatus, legal: bool) -> None:
    service = build(clock, [make_driver(1)])
    trip = service.request_ride("r1", PICKUP, DROPOFF, RideType.ECONOMY)
    if legal:
        assert service.trips.transition(trip.id, target).status is target
    else:
        with pytest.raises(TripStateError):
            service.trips.transition(trip.id, target)


def test_ride_type_eligibility_filters_the_shortlist(clock: FakeClock) -> None:
    service = build(clock, [make_driver(1), make_driver(2, {RideType.BLACK})])
    request = RideRequest("rq", "r1", PICKUP, DROPOFF, RideType.BLACK, START_EPOCH)
    assert service.matching.shortlist(request) == ["d2"]
    black = service.request_ride("r1", PICKUP, DROPOFF, RideType.BLACK)
    assert service.matching.offers_for(black.id)[0].driver_id == "d2"


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (NearestDriver(), "near"),
        (FastestEta(), "near"),
        (HighestRatedNearby(), "star"),
        (FairRotation(), "fresh"),
    ],
)
def test_matching_strategies_rank_differently(strategy: MatchingStrategy, expected: str) -> None:
    request = RideRequest("rq", "r1", PICKUP, DROPOFF, RideType.ECONOMY, START_EPOCH)
    candidates = [
        DriverSnapshot("near", Location(PICKUP.lat + 0.001, PICKUP.lon), 4.1, 9),
        DriverSnapshot("star", Location(PICKUP.lat + 0.004, PICKUP.lon), 5.0, 6),
        DriverSnapshot("fresh", Location(PICKUP.lat + 0.006, PICKUP.lon), 4.4, 0),
    ]
    assert strategy.rank(request, candidates)[0].driver_id == expected


def test_cancellation_is_free_inside_the_grace_window_and_charged_after(clock: FakeClock) -> None:
    service = build(clock, [make_driver(1), make_driver(2)])
    early = service.request_ride("r1", PICKUP, DROPOFF, RideType.ECONOMY)
    service.driver_accepts(service.matching.offers_for(early.id)[0].id, "d1")
    clock.advance(60)
    assert service.cancel_ride(early.id).cancellation_fee == Money(0)  # inside 120 s

    late = service.request_ride("r1", PICKUP, DROPOFF, RideType.ECONOMY)
    service.driver_accepts(service.matching.offers_for(late.id)[0].id, "d1")
    clock.advance(300)
    cancelled = service.cancel_ride(late.id)
    assert cancelled.cancellation_fee == Money.of("3.00")
    assert service.pay(late.id).amount == Money.of("3.00")
    assert service.matching.driver("d1").status is DriverStatus.AVAILABLE


def test_rating_an_unfinished_trip_is_rejected_and_a_finished_one_moves_the_average(clock: FakeClock) -> None:
    service = build(clock, [make_driver(1, rating=4.0)])
    service.matching.driver("d1").ratings_count = 3
    trip = service.request_ride("r1", PICKUP, DROPOFF, RideType.ECONOMY)
    with pytest.raises(TripStateError):
        service.rate_driver(trip.id, 5)
    service.driver_accepts(service.matching.offers_for(trip.id)[0].id, "d1")
    service.driver_arrived(trip.id)
    service.start_trip(trip.id)
    clock.advance(600)
    service.end_trip(trip.id, distance_km=3.0)
    service.rate_driver(trip.id, 5, "smooth")
    assert service.matching.driver("d1").rating == 4.25  # (4.0 x 3 + 5) / 4
