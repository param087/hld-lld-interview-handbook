"""Seat holds with a TTL, version-checked confirmation and a virtual waiting room.

What the module demonstrates, in the order an interviewer asks about it:

* ``SeatInventory.hold`` takes every requested seat or none of them. A seat is takeable when it
  is AVAILABLE or when the hold on it has expired. Every successful write bumps the seat's
  ``version``: that counter is the optimistic lock the rest of the flow relies on.
* ``SeatInventory.confirm`` is the payment callback. It books the seats only if each one is
  still held by this hold at the version captured when the hold was taken, the in-memory
  twin of ``UPDATE seats SET status = 'BOOKED', version = version + 1 WHERE seat_id = ? AND
  hold_id = ? AND version = ?`` followed by a row-count check. A hold that expired but whose
  seats nobody else took is still confirmable inside a grace window, which is how the
  "payment succeeded after the hold expired" race ends without a refund.
* ``WaitingRoom`` is the admission-token queue in front of an on-sale: users queue in FIFO
  order, ``admit`` hands out tokens with their own TTL at the rate the database can absorb,
  and ``hold`` refuses callers without a live token.
* ``RoomTypeInventory`` is the hotel variant: per-night counters for one room type, an
  all-or-nothing reservation across a date range and an explicit overbooking allowance.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

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


# --8<-- [start:models]
class SeatStatus(StrEnum):
    AVAILABLE = "available"
    HELD = "held"
    BOOKED = "booked"


class HoldStatus(StrEnum):
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    RELEASED = "released"  # the user gave it up or payment failed
    EXPIRED = "expired"  # the TTL ran out before payment completed


@dataclass(slots=True)
class Seat:
    seat_id: str
    status: SeatStatus = SeatStatus.AVAILABLE
    version: int = 0  # bumped by every hold, release, confirm: the optimistic lock
    hold_id: str | None = None


@dataclass(slots=True)
class Hold:
    hold_id: str
    user_id: str
    seat_ids: tuple[str, ...]
    expires_at: float
    versions: dict[str, int] = field(default_factory=dict)  # seat version after the hold
    status: HoldStatus = HoldStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class Booking:
    booking_id: str
    hold_id: str
    user_id: str
    seat_ids: tuple[str, ...]
    payment_ref: str


# --8<-- [end:models]


# --8<-- [start:waiting_room]
@dataclass(frozen=True, slots=True)
class AdmissionToken:
    user_id: str
    token: str
    expires_at: float


class WaitingRoom:
    """FIFO queue in front of an on-sale. ``_lock`` guards the queue, the ticket counters and
    the admitted map. Production keeps the queue in Redis and signs the token so the booking
    tier can verify it without a lookup."""

    def __init__(self, clock: Clock, ids: IdGenerator, token_ttl: float = 600.0) -> None:
        if token_ttl <= 0:
            raise ValidationError("token_ttl must be positive")
        self._clock = clock
        self._ids = ids
        self._token_ttl = token_ttl
        self._queue: deque[str] = deque()
        self._ticket_of: dict[str, int] = {}  # user -> ticket number while queued
        self._next_ticket = 1
        self._served = 0
        self._admitted: dict[str, AdmissionToken] = {}
        self._lock = threading.Lock()

    def join(self, user_id: str) -> int:
        """Enqueue and return the 1-based position; rejoining returns the current position."""
        with self._lock:
            if user_id not in self._ticket_of:
                self._ticket_of[user_id] = self._next_ticket
                self._next_ticket += 1
                self._queue.append(user_id)
            return self._ticket_of[user_id] - self._served

    def admit(self, count: int) -> list[AdmissionToken]:
        """Let the next ``count`` users in; the admission rate is the database's write budget."""
        if count <= 0:
            raise ValidationError("count must be positive")
        expires_at = self._clock.now() + self._token_ttl
        admitted: list[AdmissionToken] = []
        with self._lock:
            while self._queue and len(admitted) < count:
                user_id = self._queue.popleft()
                del self._ticket_of[user_id]
                self._served += 1
                token = AdmissionToken(user_id, self._ids.next_id(), expires_at)
                self._admitted[user_id] = token
                admitted.append(token)
        return admitted

    def require(self, user_id: str, token: str | None) -> None:
        """Raise unless ``token`` is the live admission token of ``user_id``."""
        with self._lock:
            issued = self._admitted.get(user_id)
        if issued is None or token != issued.token:
            raise ConflictError(f"{user_id} is not admitted")
        if issued.expires_at <= self._clock.now():
            raise ConflictError(f"admission token of {user_id} expired")

    def waiting(self) -> int:
        with self._lock:
            return len(self._queue)


# --8<-- [end:waiting_room]


# --8<-- [start:inventory]
class SeatInventory:
    """The seats of one event. ``_lock`` guards ``_seats``, ``_holds`` and ``_bookings``.

    In production the lock is the database: each method below is one transaction whose
    conditional UPDATEs touch exactly the rows asked for, or the transaction rolls back.
    """

    def __init__(
        self,
        event_id: str,
        seat_ids: Iterable[str],
        clock: Clock | None = None,
        hold_ids: IdGenerator | None = None,
        booking_ids: IdGenerator | None = None,
        hold_ttl: float = 600.0,
        grace: float = 30.0,
        waiting_room: WaitingRoom | None = None,
    ) -> None:
        if hold_ttl <= 0 or grace < 0:
            raise ValidationError("hold_ttl must be positive and grace non-negative")
        self.event_id = event_id
        self._clock = clock or SystemClock()
        self._hold_ids = hold_ids or SequentialIdGenerator("hold")
        self._booking_ids = booking_ids or SequentialIdGenerator("bk")
        self._ttl = hold_ttl
        self._grace = grace
        self._room = waiting_room
        self._seats: dict[str, Seat] = {seat_id: Seat(seat_id) for seat_id in seat_ids}
        self._holds: dict[str, Hold] = {}
        self._bookings: dict[str, Booking] = {}  # hold_id -> booking
        self._lock = threading.Lock()
        if not self._seats:
            raise ValidationError("an event needs at least one seat")

    # -- write path: hold ---------------------------------------------------------------
    def hold(self, user_id: str, seat_ids: Sequence[str], token: str | None = None) -> Hold:
        """Hold every seat or none: the multi-row conditional UPDATE of the booking service."""
        if not seat_ids or len(set(seat_ids)) != len(seat_ids):
            raise ValidationError("seat_ids must be non-empty and distinct")
        unknown = [seat_id for seat_id in seat_ids if seat_id not in self._seats]
        if unknown:
            raise NotFoundError(f"unknown seats: {unknown}")
        if self._room is not None:
            self._room.require(user_id, token)
        now = self._clock.now()
        with self._lock:
            taken = [seat_id for seat_id in seat_ids if not self._takeable(self._seats[seat_id], now)]
            if taken:
                raise ConflictError(f"not available: {taken}")  # zero rows touched: roll back
            hold = Hold(self._hold_ids.next_id(), user_id, tuple(seat_ids), now + self._ttl)
            for seat_id in seat_ids:  # an expired hold's row is not touched: its confirm will see the version move
                seat = self._seats[seat_id]
                seat.status, seat.hold_id, seat.version = SeatStatus.HELD, hold.hold_id, seat.version + 1
                hold.versions[seat_id] = seat.version
            self._holds[hold.hold_id] = hold
            return hold

    def _takeable(self, seat: Seat, now: float) -> bool:
        if seat.status is SeatStatus.AVAILABLE:
            return True
        if seat.status is SeatStatus.HELD and seat.hold_id is not None:
            return self._holds[seat.hold_id].expires_at <= now
        return False

    # -- write path: confirm and release --------------------------------------------------
    def confirm(self, hold_id: str, payment_ref: str) -> Booking:
        """Book the held seats if nothing moved since the hold: the version-checked UPDATE."""
        with self._lock:
            hold = self._get_hold(hold_id)
            if hold.status is HoldStatus.CONFIRMED:
                return self._bookings[hold_id]  # a retried payment webhook: same answer
            if hold.status is HoldStatus.RELEASED:
                raise InvalidStateError(f"hold {hold_id} was released")
            if hold.status is HoldStatus.EXPIRED or self._clock.now() > hold.expires_at + self._grace:
                self._release_seats(hold, HoldStatus.EXPIRED)
                raise ConflictError(f"hold {hold_id} expired before payment completed")
            stale = [
                seat_id
                for seat_id in hold.seat_ids
                if self._seats[seat_id].hold_id != hold_id
                or self._seats[seat_id].version != hold.versions[seat_id]
            ]
            if stale:  # someone took the seat after the TTL: rowcount < len(seats), roll back
                self._release_seats(hold, HoldStatus.EXPIRED)
                raise ConflictError(f"seats re-assigned after the hold expired: {stale}")
            for seat_id in hold.seat_ids:
                seat = self._seats[seat_id]
                seat.status, seat.version = SeatStatus.BOOKED, seat.version + 1
            hold.status = HoldStatus.CONFIRMED
            booking = Booking(self._booking_ids.next_id(), hold_id, hold.user_id, hold.seat_ids, payment_ref)
            self._bookings[hold_id] = booking
            return booking

    def release(self, hold_id: str) -> None:
        """Give the seats back (user cancelled or payment failed). Idempotent."""
        with self._lock:
            hold = self._get_hold(hold_id)
            if hold.status is HoldStatus.CONFIRMED:
                raise InvalidStateError(f"hold {hold_id} is confirmed; cancel the booking instead")
            if hold.status is HoldStatus.ACTIVE:
                self._release_seats(hold, HoldStatus.RELEASED)

    def expire_holds(self) -> int:
        """The sweeper: release holds past TTL plus grace, so a late confirm fails cleanly."""
        deadline = self._clock.now() - self._grace
        with self._lock:
            expired = [h for h in self._holds.values() if h.status is HoldStatus.ACTIVE and h.expires_at <= deadline]
            for hold in expired:
                self._release_seats(hold, HoldStatus.EXPIRED)
            return len(expired)

    def _release_seats(self, hold: Hold, status: HoldStatus) -> None:
        for seat_id in hold.seat_ids:
            seat = self._seats[seat_id]
            if seat.hold_id == hold.hold_id and seat.status is SeatStatus.HELD:
                seat.status, seat.hold_id, seat.version = SeatStatus.AVAILABLE, None, seat.version + 1
        hold.status = status

    def _get_hold(self, hold_id: str) -> Hold:
        if hold_id not in self._holds:
            raise NotFoundError(f"unknown hold {hold_id}")
        return self._holds[hold_id]

    # -- read path --------------------------------------------------------------------------
    def seat_map(self) -> dict[str, SeatStatus]:
        """What the seat-map cache serves: an expired hold already shows as available."""
        now = self._clock.now()
        with self._lock:
            return {
                seat_id: SeatStatus.AVAILABLE if self._takeable(seat, now) else seat.status
                for seat_id, seat in self._seats.items()
            }

    def available_count(self) -> int:
        return sum(1 for status in self.seat_map().values() if status is SeatStatus.AVAILABLE)


# --8<-- [end:inventory]


# --8<-- [start:hotel]
class RoomTypeInventory:
    """Hotel variant: one counter per night for a room type, a stay is all-or-nothing.

    ``_lock`` guards ``_sold`` and ``_stays``. The SQL twin is one statement,
    ``UPDATE room_night SET sold = sold + 1 WHERE room_type = ? AND night BETWEEN ? AND ?
    AND sold < allotment``, committed only if it touched exactly ``nights`` rows.
    """

    def __init__(self, room_type: str, rooms: int, overbook_pct: int = 0, ids: IdGenerator | None = None) -> None:
        if rooms <= 0 or overbook_pct < 0:
            raise ValidationError("rooms must be positive and overbook_pct non-negative")
        self.room_type = room_type
        self.allotment = rooms + rooms * overbook_pct // 100  # the overbooking policy, per night
        self._ids = ids or SequentialIdGenerator("stay")
        self._sold: dict[int, int] = defaultdict(int)  # night index -> rooms sold
        self._stays: dict[str, range] = {}
        self._lock = threading.Lock()

    def reserve(self, guest_id: str, check_in: int, nights: int) -> str:
        if nights <= 0:
            raise ValidationError("a stay needs at least one night")
        span = range(check_in, check_in + nights)
        with self._lock:
            full = [night for night in span if self._sold[night] >= self.allotment]
            if full:
                raise ConflictError(f"{self.room_type} full on nights {full}")  # rowcount < nights
            for night in span:
                self._sold[night] += 1
            stay_id = self._ids.next_id()
            self._stays[stay_id] = span
            return stay_id

    def cancel(self, stay_id: str) -> None:
        with self._lock:
            span = self._stays.pop(stay_id, None)
            for night in span or ():
                self._sold[night] -= 1

    def sold(self, night: int) -> int:
        with self._lock:
            return self._sold[night]


# --8<-- [end:hotel]


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=1_700_000_000)
    room = WaitingRoom(clock, SequentialIdGenerator("tok"), token_ttl=1800)
    seats = SeatInventory("ev-1", ["A1", "A2", "A3", "A4"], clock, hold_ttl=600, grace=30, waiting_room=room)
    for user in ("ann", "bob", "cat", "dan"):
        room.join(user)
    tokens = {t.user_id: t.token for t in room.admit(2)}
    print(f"waiting room: admitted {sorted(tokens)}, {room.waiting()} still waiting")
    try:
        seats.hold("cat", ["A1"])
    except ConflictError as exc:
        print(f"cat holds A1 without a token        -> rejected: {exc}")
    ann = seats.hold("ann", ["A1", "A2"], tokens["ann"])
    print(f"ann holds A1,A2                     -> {ann.hold_id}, versions {ann.versions}, 600 s TTL")
    try:
        seats.hold("bob", ["A2", "A3"], tokens["bob"])
    except ConflictError as exc:
        print(f"bob holds A2,A3                     -> rejected: {exc} (all or nothing, A3 untouched)")
    booking = seats.confirm(ann.hold_id, "pay_1")
    print(f"ann pays, confirm {ann.hold_id}           -> {booking.booking_id}, seats {list(booking.seat_ids)} booked")
    bob = seats.hold("bob", ["A3"], tokens["bob"])
    clock.advance(601)
    print(f"bob holds A3, 601 s pass            -> seat map {({k: v.value for k, v in seats.seat_map().items()})}")
    booking = seats.confirm(bob.hold_id, "pay_2")
    print(f"bob pays late, inside 30 s grace    -> {booking.booking_id}: nobody took A3, version check passed")
    tokens.update({t.user_id: t.token for t in room.admit(2)})
    cat = seats.hold("cat", ["A4"], tokens["cat"])
    clock.advance(601)
    dan = seats.hold("dan", ["A4"], tokens["dan"])
    print(f"cat holds A4, 601 s pass, dan holds A4 -> {dan.hold_id} took it over (version {dan.versions['A4']})")
    try:
        seats.confirm(cat.hold_id, "pay_3")
    except ConflictError as exc:
        print(f"cat pays late, confirm {cat.hold_id}      -> rejected: {exc}: refund pay_3")
    clock.advance(700)
    print(f"700 s later, sweeper                -> released {seats.expire_holds()} hold, free seats: {seats.available_count()}")

    hotel = RoomTypeInventory("deluxe", rooms=2, overbook_pct=50)
    print(f"hotel deluxe: 2 rooms, 50% overbooking -> allotment {hotel.allotment} per night")
    first = hotel.reserve("g1", check_in=10, nights=3)
    hotel.reserve("g2", check_in=11, nights=3)
    hotel.reserve("g3", check_in=12, nights=3)
    try:
        hotel.reserve("g4", check_in=11, nights=2)
    except ConflictError as exc:
        print(f"  g1 10-12, g2 11-13, g3 12-14 ok; g4 11-12 -> rejected: {exc}; night 11 stays at {hotel.sold(11)}")
    hotel.cancel(first)
    print(f"  g1 cancels, g4 retries               -> {hotel.reserve('g4', check_in=11, nights=2)}, night 12 sold {hotel.sold(12)}")


if __name__ == "__main__":
    main()
