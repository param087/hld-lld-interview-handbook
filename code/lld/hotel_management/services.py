"""Availability by date range and the front desk facade.

Two lock families, exactly as in the movie-ticket sibling:

* ``AvailabilityService._locks`` holds **one lock per room type**. It guards the per-night
  booked counters *and* the physical room roster for that type, so "sell a deluxe" and
  "assign deluxe 203" are serialised against each other.
* ``FrontDeskService._reservations_lock`` guards the reservation registry, every
  reservation state transition and the idempotency-key table. It is never held while a
  room-type lock is held.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock
from lld.hotel_management.hotel import Hotel
from lld.hotel_management.models import (
    DateRange,
    Invoice,
    InvoiceLine,
    NoAvailabilityError,
    NoRoomReadyError,
    Payment,
    PaymentDeclinedError,
    PaymentMethod,
    Reservation,
    ReservationStateError,
    ReservationStatus,
    Room,
    RoomRequest,
    RoomType,
    UnknownReservationError,
)
from lld.hotel_management.ports import AlwaysApprovesGateway, PaymentGateway, StayListener
from lld.hotel_management.strategies import (
    CancellationPolicy,
    FreeUntilDaysBefore,
    PricingStrategy,
    SeasonalPricing,
    quote_stay,
)


# --8<-- [start:availability]
class AvailabilityService:
    """Per-night counters per room type, plus the lock that makes them atomic.

    Reservations are sold against a *type*, never a room number: that is how hotels
    actually work and it keeps the contended resource a single integer per night.
    """

    def __init__(self, hotel: Hotel, overbooking: dict[RoomType, int] | None = None) -> None:
        self._hotel = hotel
        self._overbooking = overbooking or {}
        self._booked: dict[RoomType, dict[date, int]] = {}
        self._holders: dict[str, tuple[tuple[RoomRequest, ...], DateRange]] = {}
        self._locks: dict[RoomType, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def _lock_for(self, room_type: RoomType) -> threading.Lock:
        with self._registry_lock:
            return self._locks.setdefault(room_type, threading.Lock())

    @contextmanager
    def types_locked(self, room_types: Sequence[RoomType]) -> Iterator[None]:
        """Acquire one lock per room type, always in sorted order.

        A family booking a deluxe and a suite takes DELUXE before SUITE; so does every
        other caller, so the ABBA deadlock cannot happen.
        """
        acquired: list[threading.Lock] = []
        try:
            for room_type in sorted(set(room_types)):
                lock = self._lock_for(room_type)
                lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def capacity(self, room_type: RoomType) -> int:
        return self._hotel.inventory().get(room_type, 0) + self._overbooking.get(room_type, 0)

    def _free_on(self, room_type: RoomType, stay: DateRange) -> int:
        """Rooms free on the *tightest* night of the stay. Callers hold the type lock."""
        booked = self._booked.setdefault(room_type, {})
        capacity = self.capacity(room_type)
        return min(capacity - booked.get(night, 0) for night in stay.nights())

    def available(self, room_type: RoomType, stay: DateRange) -> int:
        with self.types_locked([room_type]):
            return self._free_on(room_type, stay)

    def reserve(
        self, rooms: Sequence[RoomRequest], stay: DateRange, reservation_id: str
    ) -> None:
        """All-or-nothing across every requested type and every night of the stay."""
        with self.types_locked([r.room_type for r in rooms]):
            short = [
                f"{r.count} x {r.room_type}"
                for r in rooms
                if self._free_on(r.room_type, stay) < r.count
            ]
            if short:
                raise NoAvailabilityError(f"{stay}: cannot sell {', '.join(short)}")
            for request in rooms:
                counters = self._booked.setdefault(request.room_type, {})
                for night in stay.nights():
                    counters[night] = counters.get(night, 0) + request.count
            self._holders[reservation_id] = (tuple(rooms), stay)

    def release(self, reservation_id: str) -> bool:
        """Give the nights back. Idempotent: releasing twice is a no-op, not a bug."""
        held = self._holders.get(reservation_id)
        if held is None:
            return False
        rooms, stay = held
        with self.types_locked([r.room_type for r in rooms]):
            if self._holders.pop(reservation_id, None) is None:
                return False
            for request in rooms:
                counters = self._booked[request.room_type]
                for night in stay.nights():
                    counters[night] = max(0, counters.get(night, 0) - request.count)
        return True

    def calendar(self, room_type: RoomType, stay: DateRange) -> dict[date, int]:
        """Free rooms per night - what the availability grid in the UI renders."""
        with self.types_locked([room_type]):
            booked = self._booked.setdefault(room_type, {})
            capacity = self.capacity(room_type)
            return {night: capacity - booked.get(night, 0) for night in stay.nights()}


# --8<-- [end:availability]


# --8<-- [start:front_desk]
class FrontDeskService:
    """Facade: search, reserve, pay, check in, check out, cancel, no-show sweep."""

    def __init__(
        self,
        hotel: Hotel,
        availability: AvailabilityService,
        gateway: PaymentGateway | None = None,
        pricing: PricingStrategy | None = None,
        cancellation: CancellationPolicy | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        payment_ids: IdGenerator | None = None,
    ) -> None:
        self._hotel = hotel
        self._availability = availability
        self._gateway = gateway or AlwaysApprovesGateway()
        self._pricing = pricing or SeasonalPricing()
        self._cancellation = cancellation or FreeUntilDaysBefore(2)
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("RSV")
        self._payment_ids = payment_ids or SequentialIdGenerator("PAY")
        self._reservations: dict[str, Reservation] = {}
        self._by_key: dict[str, str] = {}
        self._payments: dict[str, Payment] = {}
        self._listeners: list[StayListener] = []
        self._reservations_lock = threading.Lock()

    def subscribe(self, listener: StayListener) -> None:
        self._listeners.append(listener)

    def today(self) -> date:
        return self._clock.now_dt().date()

    def search(self, room_type: RoomType, stay: DateRange) -> int:
        return self._availability.available(room_type, stay)

    def quote(self, rooms: Sequence[RoomRequest], stay: DateRange) -> Money:
        return quote_stay(self._pricing, tuple(rooms), stay)

    def reserve(self, guest_id: str, rooms: Sequence[RoomRequest], stay: DateRange) -> Reservation:
        """Hold the nights first; only then does a reservation exist."""
        if not rooms:
            raise NoAvailabilityError("a reservation needs at least one room request")
        requests = tuple(rooms)
        reservation_id = self._ids.next_id()
        self._availability.reserve(requests, stay, reservation_id)
        reservation = Reservation(
            id=reservation_id,
            guest_id=guest_id,
            stay=stay,
            rooms=requests,
            amount=self.quote(requests, stay),
        )
        with self._reservations_lock:
            self._reservations[reservation.id] = reservation
        self._emit("reserved", reservation, None)
        return reservation

    def pay(self, reservation_id: str, method: PaymentMethod, idempotency_key: str) -> Reservation:
        """Charge outside every lock, then confirm. Replaying the key never charges twice."""
        with self._reservations_lock:
            replayed = self._by_key.get(idempotency_key)
            if replayed is not None:
                return self._require(replayed)
            reservation = self._require(reservation_id)
            if reservation.status is not ReservationStatus.PENDING:
                raise ReservationStateError(
                    f"reservation {reservation_id} is {reservation.status}, not pending"
                )
            payment = Payment(
                id=self._payment_ids.next_id(),
                reservation_id=reservation.id,
                amount=reservation.amount,
                method=method,
                idempotency_key=idempotency_key,
            )
            self._payments[payment.id] = payment
            self._by_key[idempotency_key] = reservation.id

        if not self._gateway.charge(payment.id, reservation.amount, method):
            with self._reservations_lock:
                del self._by_key[idempotency_key]
            raise PaymentDeclinedError(f"{method} payment of {reservation.amount} declined")

        with self._reservations_lock:
            payment.captured = True
            reservation.payment_id = payment.id
            reservation.transition_to(ReservationStatus.CONFIRMED)
        self._emit("confirmed", reservation, None)
        return reservation

    def check_in(self, reservation_id: str) -> tuple[str, ...]:
        """Assign physical rooms as late as possible: at the desk, under the type lock."""
        with self._reservations_lock:
            reservation = self._require(reservation_id)
            if reservation.status is not ReservationStatus.CONFIRMED:
                raise ReservationStateError(
                    f"reservation {reservation_id} is {reservation.status}, not confirmed"
                )
        assigned: list[Room] = []
        with self._availability.types_locked([r.room_type for r in reservation.rooms]):
            for request in reservation.rooms:
                for _ in range(request.count):
                    room = self._hotel.first_ready(request.room_type)
                    if room is None:
                        for taken in assigned:
                            taken.unassign()  # check-in is all-or-nothing too
                        raise NoRoomReadyError(
                            f"no clean {request.room_type} available for {reservation_id}"
                        )
                    room.occupy(reservation.id)
                    assigned.append(room)
        with self._reservations_lock:
            reservation.assigned_rooms = tuple(r.number for r in assigned)
            reservation.transition_to(ReservationStatus.CHECKED_IN)
        for room in assigned:
            self._emit("checked_in", reservation, room)
        return reservation.assigned_rooms

    def add_charge(self, reservation_id: str, description: str, amount: Money) -> None:
        with self._reservations_lock:
            reservation = self._require(reservation_id)
            if reservation.status is not ReservationStatus.CHECKED_IN:
                raise ReservationStateError("extras can only be added during the stay")
            reservation.extras.append(InvoiceLine(description, amount))

    def check_out(self, reservation_id: str) -> Invoice:
        """Vacate the rooms, release the nights, bill the stay, raise the cleaning tasks."""
        with self._reservations_lock:
            reservation = self._require(reservation_id)
            if reservation.status is not ReservationStatus.CHECKED_IN:
                raise ReservationStateError(
                    f"reservation {reservation_id} is {reservation.status}, not checked in"
                )
            invoice = self._invoice(reservation)
            reservation.transition_to(ReservationStatus.CHECKED_OUT)
            rooms = [self._hotel.room(n) for n in reservation.assigned_rooms]
        with self._availability.types_locked([r.type for r in rooms]):
            for room in rooms:
                room.vacate()
        self._availability.release(reservation.id)
        for room in rooms:
            self._emit("checked_out", reservation, room)
        return invoice

    def cancel(self, reservation_id: str) -> Money:
        today = self.today()
        with self._reservations_lock:
            reservation = self._require(reservation_id)
            refund = (
                self._cancellation.refund(reservation, today)
                if reservation.status is ReservationStatus.CONFIRMED
                else Money(0, reservation.amount.currency)
            )
            reservation.transition_to(ReservationStatus.CANCELLED)
            reservation.refunded = refund
            payment_id = reservation.payment_id
        self._availability.release(reservation.id)
        if payment_id is not None and not refund.is_zero():
            self._gateway.refund(payment_id, refund)
        self._emit("cancelled", reservation, None)
        return refund

    def sweep_no_shows(self) -> list[str]:
        """Arrival date passed and nobody checked in: free the nights for walk-ins."""
        today = self.today()
        swept: list[Reservation] = []
        with self._reservations_lock:
            for reservation in self._reservations.values():
                if (
                    reservation.status is ReservationStatus.CONFIRMED
                    and reservation.stay.start < today
                ):
                    reservation.transition_to(ReservationStatus.NO_SHOW)
                    swept.append(reservation)
        for reservation in swept:
            self._availability.release(reservation.id)
            self._emit("no_show", reservation, None)
        return [r.id for r in swept]

    def reservation(self, reservation_id: str) -> Reservation:
        with self._reservations_lock:
            return self._require(reservation_id)

    def _invoice(self, reservation: Reservation) -> Invoice:
        lines = [
            InvoiceLine(
                f"{request.count} x {request.room_type} x {reservation.stay.nights_count} nights",
                quote_stay(self._pricing, (request,), reservation.stay),
            )
            for request in reservation.rooms
        ]
        lines.extend(reservation.extras)
        subtotal = Money(0, reservation.amount.currency)
        for line in lines:
            subtotal = subtotal + line.amount
        tax = subtotal * self._hotel.tax_rate
        return Invoice(
            id=self._payment_ids.next_id(),
            reservation_id=reservation.id,
            lines=tuple(lines),
            tax=tax,
            total=subtotal + tax,
        )

    def _require(self, reservation_id: str) -> Reservation:
        try:
            return self._reservations[reservation_id]
        except KeyError:
            raise UnknownReservationError(f"unknown reservation {reservation_id!r}") from None

    def _emit(self, event: str, reservation: Reservation, room: Room | None) -> None:
        # Outside every lock: a slow listener must never stall the front desk.
        for listener in self._listeners:
            listener.on_stay_event(event, reservation, room)


# --8<-- [end:front_desk]
