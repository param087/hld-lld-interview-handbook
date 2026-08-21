from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, InvalidStateError, NotFoundError, ValidationError
from hld.presence_pubsub import (
    ChannelBus,
    LocationCache,
    PresenceServer,
    PresenceService,
)

# Union Square, the Ferry Building (~1.5 km away) and downtown Oakland (~13 km away).
UNION_SQUARE = (37.7880, -122.4075)
FERRY_BUILDING = (37.7955, -122.3937)
OAKLAND = (37.8044, -122.2712)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000.0)


@pytest.fixture
def world(clock: FakeClock) -> tuple[LocationCache, ChannelBus, PresenceService]:
    cache = LocationCache(clock, ttl_s=600)
    bus = ChannelBus()
    service = PresenceService(cache, bus, clock)
    for friend in ("bob", "cat", "dan"):
        service.befriend("ann", friend)
    for user in ("ann", "bob", "cat", "dan"):
        service.set_sharing(user, True)
    return cache, bus, service


def test_one_update_costs_one_delivery_per_server_not_per_friend(world) -> None:
    _, bus, service = world
    ws1 = PresenceServer("ws-1", bus, service)
    ws2 = PresenceServer("ws-2", bus, service)
    ws1.connect("ann")
    ws2.connect("bob")
    ws2.connect("cat")  # two friends of ann on the same server
    ws2.connect("dan")  # three friends of ann on the same server
    assert ws2.channels() == ["ann"]  # subscribed once, not once per watcher
    assert bus.subscriber_count("ann") == 1
    for user in ("bob", "cat", "dan"):
        ws2.report(user, *FERRY_BUILDING)
    result = ws1.report("ann", *UNION_SQUARE)
    assert result.servers_reached == 1
    assert [len(ws2.outbox(u)) for u in ("bob", "cat", "dan")] == [1, 1, 1]


def test_radius_filter_runs_on_the_server_that_holds_the_watcher(world) -> None:
    _, bus, service = world
    ws1 = PresenceServer("ws-1", bus, service, radius_km=8.0)
    ws2 = PresenceServer("ws-2", bus, service, radius_km=8.0)
    ws1.connect("ann")
    ws2.connect("bob")
    ws2.connect("cat")
    ws2.report("bob", *FERRY_BUILDING)
    ws2.report("cat", *OAKLAND)
    ws1.report("ann", *UNION_SQUARE)
    near = ws2.outbox("bob")
    assert len(near) == 1 and near[0].friend.user_id == "ann"
    assert 1.0 < near[0].distance_km < 2.0
    assert ws2.outbox("cat") == []  # 13 km away: the coordinate never reaches the socket


def test_a_watcher_without_a_position_yet_is_skipped(world) -> None:
    _, bus, service = world
    ws1 = PresenceServer("ws-1", bus, service)
    ws2 = PresenceServer("ws-2", bus, service)
    ws1.connect("ann")
    ws2.connect("bob")  # bob's client has not sent its first frame
    ws1.report("ann", *UNION_SQUARE)
    assert ws2.outbox("bob") == []
    ws2.report("bob", *FERRY_BUILDING)
    ws1.report("ann", *UNION_SQUARE)
    assert len(ws2.outbox("bob")) == 1


def test_expiry_is_the_offline_signal(world, clock: FakeClock) -> None:
    cache, bus, service = world
    ws1 = PresenceServer("ws-1", bus, service)
    ws1.connect("ann")
    ws1.report("ann", *UNION_SQUARE)
    ws1.connect("bob")
    ws1.report("bob", *FERRY_BUILDING)
    assert [e.friend.user_id for e in service.nearby("ann")] == ["bob"]
    clock.advance(400)
    ws1.report("ann", *UNION_SQUARE)  # ann keeps ticking, bob goes quiet
    assert cache.live_users() == ["ann", "bob"]  # 400 s < 600 s TTL
    clock.advance(300)
    ws1.report("ann", *UNION_SQUARE)
    assert cache.live_users() == ["ann"]  # bob's key expired; nobody wrote a tombstone
    assert service.nearby("ann") == []


def test_opt_out_blocks_the_write_and_drops_the_key(world) -> None:
    cache, bus, service = world
    ws1 = PresenceServer("ws-1", bus, service)
    ws1.connect("cat")
    ws1.report("cat", *OAKLAND)
    assert cache.get("cat") is not None
    service.set_sharing("cat", False)
    assert cache.get("cat") is None  # opting out deletes, it does not wait out the TTL
    with pytest.raises(InvalidStateError):
        ws1.report("cat", *OAKLAND)
    service.set_sharing("cat", True)
    assert ws1.report("cat", *OAKLAND).location.lat == pytest.approx(OAKLAND[0])


def test_disconnect_unsubscribes_only_when_the_last_watcher_leaves(world) -> None:
    _, bus, service = world
    ws2 = PresenceServer("ws-2", bus, service)
    ws2.connect("bob")
    ws2.connect("cat")
    assert bus.subscriber_count("ann") == 1
    ws2.disconnect("bob")
    assert bus.subscriber_count("ann") == 1  # cat still watches ann
    assert ws2.channels() == ["ann"]
    ws2.disconnect("cat")
    assert bus.subscriber_count("ann") == 0  # nobody here cares: drop the subscription
    assert ws2.channels() == []


def test_nearby_sorts_by_distance_and_ignores_friends_without_a_live_key(world) -> None:
    _, bus, service = world
    ws1 = PresenceServer("ws-1", bus, service)
    for user in ("ann", "bob", "cat"):
        ws1.connect(user)
        ws1.report(user, *{"ann": UNION_SQUARE, "bob": FERRY_BUILDING, "cat": OAKLAND}[user])
    wide = service.nearby("ann", radius_km=20)
    assert [e.friend.user_id for e in wide] == ["bob", "cat"]  # nearest first
    assert wide[0].distance_km < wide[1].distance_km
    assert [e.friend.user_id for e in service.nearby("ann", radius_km=8)] == ["bob"]
    assert "dan" not in {e.friend.user_id for e in wide}  # opted in, never reported


def test_validation_and_lookup_errors(world) -> None:
    cache, bus, service = world
    ws1 = PresenceServer("ws-1", bus, service)
    ws1.connect("ann")
    with pytest.raises(ValidationError):
        ws1.report("ann", 91.0, -122.0)
    with pytest.raises(NotFoundError):
        ws1.report("zed", *UNION_SQUARE)  # no socket on this server
    with pytest.raises(NotFoundError):
        service.update_location("zed", *UNION_SQUARE)
    with pytest.raises(NotFoundError):
        service.nearby("ann")  # ann has no live location yet
    with pytest.raises(ValidationError):
        service.befriend("ann", "ann")
    with pytest.raises(ValidationError):
        LocationCache(clock=None, ttl_s=0)
    with pytest.raises(ValidationError):
        PresenceServer("ws-bad", bus, service, radius_km=0)
    assert cache.get("nobody") is None


def test_concurrent_reports_all_reach_the_watcher(world) -> None:
    _, bus, service = world
    spokes = [f"spoke-{i}" for i in range(8)]
    for spoke in spokes:
        service.befriend("hub", spoke)
        service.set_sharing(spoke, True)
    service.set_sharing("hub", True)
    ws1 = PresenceServer("ws-1", bus, service)
    ws2 = PresenceServer("ws-2", bus, service)
    ws2.connect("hub")
    ws2.report("hub", *UNION_SQUARE)
    for spoke in spokes:
        ws1.connect(spoke)
        ws1.report(spoke, *UNION_SQUARE)
    assert bus.subscriber_count("hub") == 1  # ws-1 subscribed once for all eight spokes

    def tick(job: tuple[str, int]) -> int:
        spoke, step = job
        return ws1.report(spoke, UNION_SQUARE[0] + step * 1e-5, UNION_SQUARE[1]).servers_reached

    jobs = [(spoke, step) for spoke in spokes for step in range(1, 51)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        reached = list(pool.map(tick, jobs))
    assert set(reached) == {1}
    events = ws2.outbox("hub")
    assert len(events) == len(spokes) * 50 + len(spokes)  # the setup frames plus the concurrent ones
    assert {e.friend.user_id for e in events} == set(spokes)
