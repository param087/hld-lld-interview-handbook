from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, FakeClock, InvalidStateError, NotFoundError, ValidationError
from hld.dispatch_matching import DispatchService, TripState

PICKUP = (37.7880, -122.4075)  # Union Square, San Francisco


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_700_000_000.0)


@pytest.fixture
def dispatch(clock: FakeClock) -> DispatchService:
    return DispatchService(offer_timeout_s=15.0, initial_radius_km=1.0, max_radius_km=4.0, clock=clock)


def test_nearest_k_is_sorted_and_expands_the_radius(dispatch: DispatchService) -> None:
    dispatch.go_online("near", 37.7884, -122.4078)  # ~50 m
    dispatch.go_online("mid", 37.7955, -122.3937)  # ~1.6 km, outside the 1 km start radius
    dispatch.go_online("far", 37.4419, -122.1430)  # Palo Alto, ~50 km
    assert [h.item_id for h in dispatch.nearest_available(*PICKUP, k=3)] == ["near"]
    dispatch.go_offline("near")
    hits = dispatch.nearest_available(*PICKUP, k=3)
    assert [h.item_id for h in hits] == ["mid"]  # expansion found it, Palo Alto stayed out
    assert 1.0 < hits[0].distance_km < 4.0


def test_a_lease_stops_the_same_driver_being_dispatched_twice(dispatch: DispatchService) -> None:
    dispatch.go_online("d1", 37.7884, -122.4078)
    dispatch.go_online("d2", 37.7844, -122.4078)
    first = dispatch.request_ride("r1", *PICKUP)
    assert first.state is TripState.OFFERED
    leased = dispatch.outstanding_offers()[0].driver_id
    assert leased not in [h.item_id for h in dispatch.nearest_available(*PICKUP, k=5)]
    second = dispatch.request_ride("r2", *PICKUP)
    offers = {o.driver_id: o.trip_id for o in dispatch.outstanding_offers()}
    assert set(offers.values()) == {first.id, second.id}
    assert len(offers) == 2  # two trips, two distinct drivers


def test_expired_offer_is_reaped_and_the_trip_is_re_offered(
    dispatch: DispatchService, clock: FakeClock
) -> None:
    dispatch.go_online("d1", 37.7884, -122.4078)
    dispatch.go_online("d2", 37.7844, -122.4078)
    trip = dispatch.request_ride("r1", *PICKUP)
    ignored = dispatch.outstanding_offers()[0].driver_id
    clock.advance(16.0)
    assert dispatch.reap_expired_offers() == [ignored]
    assert trip.state is TripState.OFFERED and trip.offers == 2
    second = dispatch.outstanding_offers()[0]
    assert second.driver_id != ignored
    with pytest.raises(ConflictError):
        dispatch.accept_offer(ignored, trip.id)  # the stale offer cannot be honoured
    assert dispatch.accept_offer(second.driver_id, trip.id).state is TripState.ASSIGNED


def test_declined_driver_is_not_offered_the_same_trip_again(dispatch: DispatchService) -> None:
    dispatch.go_online("d1", 37.7884, -122.4078)
    dispatch.go_online("d2", 37.7844, -122.4078)
    trip = dispatch.request_ride("r1", *PICKUP)
    first = dispatch.outstanding_offers()[0].driver_id
    dispatch.decline_offer(first, trip.id)
    assert trip.state is TripState.OFFERED and trip.offers == 2
    second = dispatch.outstanding_offers()[0].driver_id
    assert second != first
    dispatch.decline_offer(second, trip.id)
    assert trip.state is TripState.UNFULFILLED  # both drivers said no, nobody is left


def test_trip_lifecycle_and_illegal_transitions(dispatch: DispatchService) -> None:
    dispatch.go_online("d1", 37.7884, -122.4078)
    trip = dispatch.request_ride("r1", *PICKUP)
    with pytest.raises(InvalidStateError):
        dispatch.complete_trip(trip.id)  # cannot complete a trip nobody accepted
    dispatch.accept_offer("d1", trip.id)
    assert dispatch.nearest_available(*PICKUP, k=5) == []  # on a trip, not dispatchable
    dispatch.start_trip(trip.id)
    assert dispatch.complete_trip(trip.id).state is TripState.COMPLETED
    assert [h.item_id for h in dispatch.nearest_available(*PICKUP, k=5)] == ["d1"]
    with pytest.raises(InvalidStateError):
        dispatch.cancel_trip(trip.id)  # completed is terminal


def test_no_supply_leaves_the_trip_unfulfilled(dispatch: DispatchService) -> None:
    dispatch.go_online("far", 37.4419, -122.1430)
    trip = dispatch.request_ride("r1", *PICKUP)
    assert trip.state is TripState.UNFULFILLED and trip.offers == 0
    assert dispatch.outstanding_offers() == []


def test_surge_is_demand_over_supply_in_the_pickup_cell(dispatch: DispatchService) -> None:
    for i in range(4):
        dispatch.go_online(f"d{i}", 37.7884 + i * 0.0005, -122.4078)
    assert dispatch.surge_multiplier(*PICKUP) == 1.0  # supply, no demand yet
    for i in range(4):
        dispatch.request_ride(f"r{i}", *PICKUP)
    demand, supply = dispatch.cell_load(*PICKUP)
    assert (demand, supply) == (4, 0)  # every driver is leased by an outstanding offer
    assert dispatch.surge_multiplier(*PICKUP, cap=10.0) == 4.0


@pytest.mark.parametrize(
    ("call", "args", "error"),
    [
        ("update_location", ("ghost", 37.79, -122.41), NotFoundError),
        ("go_online", ("d1", 99.0, -122.41), ValidationError),
        ("nearest_available", (37.79, -122.41, 0), ValidationError),
        ("trip", ("trip-404",), NotFoundError),
        ("accept_offer", ("d1", "trip-404"), ConflictError),
    ],
)
def test_input_validation(dispatch: DispatchService, call: str, args: tuple, error: type) -> None:
    with pytest.raises(error):
        getattr(dispatch, call)(*args)


def test_concurrent_requests_never_dispatch_one_driver_twice(dispatch: DispatchService) -> None:
    for i in range(40):
        dispatch.go_online(f"d{i}", 37.7884 + i * 0.0001, -122.4078)
    with ThreadPoolExecutor(max_workers=8) as pool:
        trips = list(pool.map(lambda i: dispatch.request_ride(f"r{i}", *PICKUP), range(40)))
    offers = dispatch.outstanding_offers()
    assert len(offers) == 40
    assert len({o.driver_id for o in offers}) == 40  # one lease per driver, no double dispatch
    assert {o.trip_id for o in offers} == {t.id for t in trips}
    assert all(t.state is TripState.OFFERED for t in trips)
