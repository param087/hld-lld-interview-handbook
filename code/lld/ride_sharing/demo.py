"""Six drivers on a 1 km grid: request, offer, timeout, decline, accept, ride, pay."""

from common import FakeClock, SequentialIdGenerator
from lld.ride_sharing.facade import RideHailingService
from lld.ride_sharing.index import DriverLocationIndex
from lld.ride_sharing.models import Driver, Location, Rider, RideType, Vehicle
from lld.ride_sharing.strategies import FastestEta, ZoneSurge

START_EPOCH = 1_772_020_800.0  # 2026-02-25T12:00Z
PICKUP = Location(12.9716, 77.5946)
DROPOFF = Location(13.0100, 77.6200)

FLEET = [
    ("d1", "Asha", 12.9722, 77.5952, 4.9, 80, {RideType.ECONOMY, RideType.COMFORT}),
    ("d2", "Bala", 12.9740, 77.5980, 4.4, 40, {RideType.ECONOMY}),
    ("d3", "Chetan", 12.9760, 77.6010, 4.7, 9, {RideType.ECONOMY}),
    ("d4", "Divya", 13.0500, 77.7000, 5.0, 12, {RideType.ECONOMY}),
    ("d5", "Eshan", 12.9725, 77.5949, 4.8, 60, {RideType.BLACK}),
    ("d6", "Farah", 12.9800, 77.6050, 4.2, 25, {RideType.XL}),
]


def build(clock: FakeClock) -> RideHailingService:
    index = DriverLocationIndex(cell_size_km=1.0, reference_lat=12.97, stripes=8)
    drivers = [
        Driver(
            did, name, Vehicle(f"KA-{did}", "hatchback", 4, frozenset(types)),
            Location(lat, lon), rating=rating, ratings_count=count,
        )
        for did, name, lat, lon, rating, count, types in FLEET
    ]
    surge = ZoneSurge(index, {index.cell_of(PICKUP): 1.4})
    service = RideHailingService(
        [Rider("r1", "Rider One")], drivers, index=index, strategy=FastestEta(), surge=surge,
        clock=clock, ids=SequentialIdGenerator("T"), offer_timeout=15.0,
    )
    for driver in drivers:
        service.matching.go_online(driver.id)
    return service


def main() -> None:
    clock = FakeClock(start=START_EPOCH)
    service = build(clock)
    index = service.matching.index
    print(f"{index.size()} drivers online; pickup cell {index.cell_of(PICKUP)} holds {index.cell_population(index.cell_of(PICKUP))}")
    print(f"within 1.5 km of the pickup: {sorted(index.nearby(PICKUP, 1.5))}")
    print(f"economy estimate: {service.estimate(PICKUP, DROPOFF, RideType.ECONOMY)}")

    trip = service.request_ride("r1", PICKUP, DROPOFF, RideType.ECONOMY)
    first = service.matching.offers_for(trip.id)[0]
    print(f"{trip.id} requested; shortlist ranked by ETA, {first.id} offered to {first.driver_id} for 15 s")

    clock.advance(16)
    expired = service.sweep_offers()
    second = service.matching.offers_for(trip.id)[-1]
    print(f"{expired[0].id} {expired[0].status}; cascaded to {second.driver_id}, who declines")
    third = service.driver_declines(second.id, second.driver_id)
    assert third is not None
    print(f"{third.id} offered to {third.driver_id}, who accepts")
    service.driver_accepts(third.id, third.driver_id)

    service.driver_arrived(trip.id)
    service.start_trip(trip.id)
    clock.advance(18 * 60)
    completed = service.end_trip(trip.id, distance_km=6.4)
    print(f"{trip.id} {completed.status} after 6.4 km in {completed.minutes():.0f} min")
    print(f"fare: {completed.fare}")
    payment = service.pay(trip.id)
    service.rate_driver(trip.id, 5)
    driver = service.matching.driver(third.driver_id)
    print(f"{payment.id} captured {payment.amount}; {driver.name} now rated {driver.rating} with {service.earnings.earnings(driver.id)} earned")
    print(f"timeline: {' -> '.join(service.feed.timeline(trip.id))}")


if __name__ == "__main__":
    main()
