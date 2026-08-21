"""Nearby Friends: per-user location channels, friend subscriptions and a TTL location cache.

The crux of the design in one module:

* ``LocationCache`` is the only place a coordinate is stored: ``SET location:{user_id} ... EX 600``
  in Redis. An entry nobody refreshes expires by itself, so "stopped sharing" needs no event and
  no tombstone -- expiry *is* the offline signal.
* ``ChannelBus`` is per-user pub/sub. A user publishes once to ``channel:{user_id}``; subscribers
  are the *servers* holding at least one of that user's friends, so 200 friends spread over 20
  servers cost 20 deliveries instead of 200.
* ``PresenceServer`` subscribes to a friend's channel once per server, keeps the interest map
  ``friend -> local watchers``, and runs the radius filter locally against its own sockets'
  positions. The bus carries coordinates; the server decides who is close enough to be told.
* ``PresenceService`` owns the mutual friend graph and the opt-in flag, and refuses to publish a
  coordinate for a user who has not turned sharing on.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from common import Clock, InvalidStateError, NotFoundError, SystemClock, ValidationError
from hld.geohash import haversine_km

DEFAULT_TTL_S = 600.0  # 10 minutes: a location nobody refreshed is stale, not wrong
DEFAULT_RADIUS_KM = 8.0  # what the product calls "nearby"


def _validate_point(lat: float, lon: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValidationError(f"({lat}, {lon}) is not a valid latitude/longitude pair")


# --8<-- [start:models]
@dataclass(frozen=True, slots=True)
class Location:
    """One position report. ``at`` is when the client sampled it, not when the server saw it."""

    user_id: str
    lat: float
    lon: float
    at: float


@dataclass(frozen=True, slots=True)
class NearbyEvent:
    """What a watcher's socket receives: a friend's new position and the distance to it."""

    friend: Location
    distance_km: float


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """What the write path reports back: what was cached and how far the publish travelled."""

    location: Location
    servers_reached: int


# --8<-- [end:models]


# --8<-- [start:cache]
class LocationCache:
    """Last known location per user, with a TTL. ``SET location:{id} <blob> EX 600`` in Redis.

    There is no "user went offline" write anywhere in this design: a client that stops sending
    updates simply stops refreshing its key, and the key expires. ``_lock`` protects ``_entries``.
    """

    def __init__(self, clock: Clock | None = None, ttl_s: float = DEFAULT_TTL_S) -> None:
        if ttl_s <= 0:
            raise ValidationError("ttl_s must be positive")
        self._clock = clock or SystemClock()
        self._ttl = ttl_s
        self._entries: dict[str, tuple[Location, float]] = {}  # user -> (location, written_at)
        self._lock = threading.Lock()

    @property
    def ttl_s(self) -> float:
        return self._ttl

    def put(self, location: Location) -> None:
        with self._lock:
            self._entries[location.user_id] = (location, self._clock.now())

    def get(self, user_id: str) -> Location | None:
        """``None`` once the entry is older than the TTL; eviction is lazy, exactly like Redis."""
        now = self._clock.now()
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is None:
                return None
            location, written_at = entry
            if now - written_at > self._ttl:
                del self._entries[user_id]
                return None
            return location

    def forget(self, user_id: str) -> None:
        """Sharing turned off: drop the key now instead of waiting out the TTL."""
        with self._lock:
            self._entries.pop(user_id, None)

    def live_users(self) -> list[str]:
        with self._lock:
            snapshot = list(self._entries)
        return sorted(user_id for user_id in snapshot if self.get(user_id) is not None)


# --8<-- [end:cache]


# --8<-- [start:bus]
class ChannelBus:
    """Per-user pub/sub: ``PUBLISH channel:{user_id}`` reaches every subscribed server once.

    Subscribers are servers, not sockets. This is the whole reason the fan-out is affordable: the
    number of deliveries per update is the number of *servers* holding a friend, which is bounded
    by the fleet size, not by how many friends the user has. ``_lock`` protects ``_subscribers``.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, dict[str, Callable[[Location], None]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, channel: str, subscriber_id: str, handler: Callable[[Location], None]) -> None:
        with self._lock:
            self._subscribers.setdefault(channel, {})[subscriber_id] = handler

    def unsubscribe(self, channel: str, subscriber_id: str) -> None:
        with self._lock:
            handlers = self._subscribers.get(channel)
            if handlers is None:
                return
            handlers.pop(subscriber_id, None)
            if not handlers:
                del self._subscribers[channel]

    def subscriber_count(self, channel: str) -> int:
        with self._lock:
            return len(self._subscribers.get(channel, {}))

    def publish(self, channel: str, location: Location) -> int:
        """Deliver to every subscriber; the return value is the fan-out width of this update.

        Zero means nobody is watching this user right now, which is the common case: most people
        have no friend with the app open, and the update stops at the cache.
        """
        with self._lock:
            handlers = list(self._subscribers.get(channel, {}).values())
        for handler in handlers:
            handler(location)
        return len(handlers)


# --8<-- [end:bus]


# --8<-- [start:server]
class PresenceServer:
    """One stateful WebSocket server: it owns the sockets connected to it, and nothing else.

    Two invariants make the design scale. It subscribes to a friend's channel **once**, however
    many of that friend's friends it happens to hold (``_interest`` counts the watchers). And it
    keeps the last position of its *own* users in ``_local``, so the radius filter costs no cache
    read: the watcher's socket terminates here, so this server already saw its coordinate.
    ``_lock`` protects ``_interest``, ``_local`` and ``_outboxes``.
    """

    def __init__(
        self,
        server_id: str,
        bus: ChannelBus,
        service: PresenceService,
        radius_km: float = DEFAULT_RADIUS_KM,
    ) -> None:
        if radius_km <= 0:
            raise ValidationError("radius_km must be positive")
        self.server_id = server_id
        self._bus = bus
        self._service = service
        self._radius = radius_km
        self._interest: dict[str, set[str]] = {}  # friend channel -> local users watching it
        self._local: dict[str, Location] = {}  # last position reported by a local socket
        self._outboxes: dict[str, list[NearbyEvent]] = {}  # local user -> frames written
        self._lock = threading.Lock()

    def connect(self, user_id: str) -> None:
        """A socket opens: register an outbox and subscribe to each friend's channel once."""
        friends = self._service.friends_of(user_id)
        with self._lock:
            self._outboxes.setdefault(user_id, [])
            fresh = [f for f in friends if not self._interest.get(f)]
            for friend in friends:
                self._interest.setdefault(friend, set()).add(user_id)
        for friend in fresh:
            self._bus.subscribe(friend, self.server_id, self._on_update)

    def disconnect(self, user_id: str) -> None:
        """A socket closes: drop the outbox and unsubscribe from channels nobody here watches."""
        with self._lock:
            self._outboxes.pop(user_id, None)
            self._local.pop(user_id, None)
            dropped = []
            for friend, watchers in list(self._interest.items()):
                watchers.discard(user_id)
                if not watchers:
                    del self._interest[friend]
                    dropped.append(friend)
        for friend in dropped:
            self._bus.unsubscribe(friend, self.server_id)

    def report(self, user_id: str, lat: float, lon: float) -> UpdateResult:
        """A location frame arrives on a local socket: cache it, publish it once, remember it."""
        with self._lock:
            if user_id not in self._outboxes:
                raise NotFoundError(f"{user_id} has no socket on {self.server_id}")
        result = self._service.update_location(user_id, lat, lon)
        with self._lock:
            self._local[user_id] = result.location
        return result

    def _on_update(self, location: Location) -> None:
        """Runs once per update per server, whatever the number of watchers it holds. The filter is
        local because the alternative -- shipping every friend's coordinate to every client and
        filtering there -- leaks the exact position of people who are far away."""
        with self._lock:
            watchers = sorted(self._interest.get(location.user_id, ()))
            positions = {w: self._local.get(w) for w in watchers}
        events: list[tuple[str, NearbyEvent]] = []
        for watcher, mine in positions.items():
            if mine is None:
                continue  # this watcher has not reported a position yet: nothing to measure from
            distance = haversine_km(mine.lat, mine.lon, location.lat, location.lon)
            if distance <= self._radius:
                events.append((watcher, NearbyEvent(location, distance)))
        with self._lock:
            for watcher, event in events:
                outbox = self._outboxes.get(watcher)
                if outbox is not None:
                    outbox.append(event)  # else the socket closed mid-fan-out; the client re-reads

    def outbox(self, user_id: str) -> list[NearbyEvent]:
        with self._lock:
            return list(self._outboxes.get(user_id, ()))

    def channels(self) -> list[str]:
        """Channels this server subscribes to: the deduplicated union of its users' friends."""
        with self._lock:
            return sorted(self._interest)


# --8<-- [end:server]


# --8<-- [start:service]
class PresenceService:
    """Opt-in, the mutual friend graph, and the two paths a coordinate can travel.

    The write path is ``update_location``: one cache write plus one publish, both O(1) for the
    caller. The read path is ``nearby``, used when the app opens and the push stream has told the
    client nothing yet: it fans out on read over the friend list and reads the cache. ``_lock``
    protects ``_friends`` and ``_sharing``.
    """

    def __init__(self, cache: LocationCache, bus: ChannelBus, clock: Clock | None = None) -> None:
        self._cache = cache
        self._bus = bus
        self._clock = clock or SystemClock()
        self._friends: dict[str, set[str]] = {}
        self._sharing: dict[str, bool] = {}
        self._lock = threading.Lock()

    def befriend(self, left: str, right: str) -> None:
        """Friendship is symmetric, which is what makes per-user channels work: a subscription to
        a friend's channel is always legitimate, so the bus needs no per-message authorisation."""
        if left == right:
            raise ValidationError("a user cannot befriend themselves")
        with self._lock:
            self._friends.setdefault(left, set()).add(right)
            self._friends.setdefault(right, set()).add(left)
            self._sharing.setdefault(left, False)
            self._sharing.setdefault(right, False)

    def friends_of(self, user_id: str) -> list[str]:
        with self._lock:
            if user_id not in self._friends:
                raise NotFoundError(f"unknown user {user_id!r}")
            return sorted(self._friends[user_id])

    def set_sharing(self, user_id: str, sharing: bool) -> None:
        """Opt-in is a hard gate, not a display filter: turning it off deletes the cached key."""
        with self._lock:
            if user_id not in self._friends:
                raise NotFoundError(f"unknown user {user_id!r}")
            self._sharing[user_id] = sharing
        if not sharing:
            self._cache.forget(user_id)

    def is_sharing(self, user_id: str) -> bool:
        with self._lock:
            return self._sharing.get(user_id, False)

    # -- write path ------------------------------------------------------------------
    def update_location(self, user_id: str, lat: float, lon: float) -> UpdateResult:
        """Cache the coordinate, then publish it once. Both steps are O(1) for the sender."""
        _validate_point(lat, lon)
        with self._lock:
            if user_id not in self._friends:
                raise NotFoundError(f"unknown user {user_id!r}")
            if not self._sharing[user_id]:
                raise InvalidStateError(f"{user_id} has not opted in to location sharing")
        location = Location(user_id, lat, lon, self._clock.now())
        self._cache.put(location)
        return UpdateResult(location, self._bus.publish(user_id, location))

    # -- read path -------------------------------------------------------------------
    def nearby(self, user_id: str, radius_km: float = DEFAULT_RADIUS_KM) -> list[NearbyEvent]:
        """Cold start: friends whose cached location is still alive and within the radius. Fan-out
        on read over one friend list -- a few hundred cache reads -- called on app open, before
        the push stream has told the client anything."""
        if radius_km <= 0:
            raise ValidationError("radius_km must be positive")
        mine = self._cache.get(user_id)
        if mine is None:
            raise NotFoundError(f"no live location for {user_id!r}")
        hits: list[NearbyEvent] = []
        for friend in self.friends_of(user_id):
            theirs = self._cache.get(friend)
            if theirs is None:
                continue  # not sharing, or the TTL expired: absence is the answer
            distance = haversine_km(mine.lat, mine.lon, theirs.lat, theirs.lon)
            if distance <= radius_km:
                hits.append(NearbyEvent(theirs, distance))
        hits.sort(key=lambda event: (event.distance_km, event.friend.user_id))
        return hits


# --8<-- [end:service]


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=1_700_000_000)
    cache = LocationCache(clock, ttl_s=600)
    bus = ChannelBus()
    service = PresenceService(cache, bus, clock)
    for friend in ("bob", "cat", "dan"):
        service.befriend("ann", friend)
    for user in ("ann", "bob", "cat", "dan"):
        service.set_sharing(user, True)

    ws1 = PresenceServer("ws-1", bus, service, radius_km=8.0)
    ws2 = PresenceServer("ws-2", bus, service, radius_km=8.0)
    ws1.connect("ann")
    ws2.connect("bob")
    ws2.connect("cat")  # dan keeps the app closed
    print(f"ws-1 subscribes to {ws1.channels()}; ws-2 subscribes to {ws2.channels()}")

    # Union Square, the Ferry Building (1.4 km away) and downtown Oakland (13 km away).
    ws2.report("bob", 37.7955, -122.3937)
    ws2.report("cat", 37.8044, -122.2712)
    ann = ws1.report("ann", 37.7880, -122.4075)
    print(f"ann's update: 1 publish -> {ann.servers_reached} server for 3 friends")
    print("bob's socket:", [f"{e.friend.user_id} at {e.distance_km:.1f} km" for e in ws2.outbox("bob")])
    print("cat's socket:", ws2.outbox("cat"), "-> 13 km away, filtered on the server")

    ws2.report("bob", 37.7955, -122.3937)  # bob's next 30 s tick, now that ann has a position
    print("ann's socket:", [f"{e.friend.user_id} at {e.distance_km:.1f} km" for e in ws1.outbox("ann")])
    print("ann on app open:", [(e.friend.user_id, round(e.distance_km, 1)) for e in service.nearby("ann")])

    clock.advance(700)  # bob and cat go quiet for 11 minutes; ann keeps ticking
    ws1.report("ann", 37.7880, -122.4075)
    print("after 700 s of silence, live keys =", cache.live_users())
    print("ann on app open:", service.nearby("ann"), "-> expiry is the offline signal")

    service.set_sharing("cat", False)
    try:
        ws2.report("cat", 37.8044, -122.2712)
    except InvalidStateError as exc:
        print("opt-out:", exc)

    ws2.disconnect("bob")
    ws2.disconnect("cat")
    quiet = ws1.report("ann", 37.7880, -122.4075)
    print(f"ws-2 emptied: ann's update now reaches {quiet.servers_reached} servers, so it stops at the cache")


if __name__ == "__main__":
    main()
