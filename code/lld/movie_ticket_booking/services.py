"""Seat locking, the booking facade and the hold-expiry sweeper.

Two lock families, and knowing which is which is the whole concurrency answer:

* ``SeatLockService`` owns **one lock per ShowSeat**. Multi-seat operations acquire them
  in sorted key order, so two users asking for overlapping seats can never deadlock.
* ``BookingService._bookings_lock`` guards the booking registry, the booking state machine
  and the idempotency-key table. It is never held while a seat lock is held.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock
from lld.movie_ticket_booking.catalog import Catalog
from lld.movie_ticket_booking.models import (
    Booking,
    BookingStateError,
    BookingStatus,
    HoldExpiredError,
    Payment,
    PaymentDeclinedError,
    PaymentInFlightError,
    PaymentMethod,
    PaymentStatus,
    SeatStatus,
    SeatUnavailableError,
    Show,
    validate_seat_request,
)
from lld.movie_ticket_booking.ports import (
    AlwaysApprovesGateway,
    BookingListener,
    PaymentGateway,
)
from lld.movie_ticket_booking.strategies import (
    PricingStrategy,
    RefundPolicy,
    SeatTypePricing,
    TieredRefundPolicy,
)


# --8<-- [start:seat_locks]
class SeatLockService:
    """The scarce-inventory gatekeeper: one lock per seat, taken in a fixed order.

    Created once and injected (not a Singleton class): tests build several instances,
    and a second cinema chain is a second object rather than a redesign.
    """

    DEFAULT_HOLD_TTL_SECONDS = 10 * 60

    def __init__(
        self, clock: Clock | None = None, hold_ttl_seconds: int = DEFAULT_HOLD_TTL_SECONDS
    ) -> None:
        self._clock = clock or SystemClock()
        self._ttl = hold_ttl_seconds
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def _lock_for(self, key: str) -> threading.Lock:
        with self._registry_lock:
            return self._locks.setdefault(key, threading.Lock())

    @contextmanager
    def seats_locked(self, show_id: str, seat_numbers: Sequence[str]) -> Iterator[None]:
        """Acquire every seat lock in sorted key order; release in reverse.

        Sorting is what makes the all-or-nothing hold deadlock-free: two callers
        wanting A5+A6 and A6+A5 both take A5 first, so one simply waits.
        """
        keys = sorted({f"{show_id}::{number}" for number in seat_numbers})
        acquired: list[threading.Lock] = []
        try:
            for key in keys:
                lock = self._lock_for(key)
                lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def hold(self, show: Show, seat_numbers: Sequence[str], booking_id: str) -> float:
        """All-or-nothing: check every seat, then mutate every seat. Returns the expiry."""
        with self.seats_locked(show.id, seat_numbers):
            now = self._clock.now()
            taken = sorted(n for n in seat_numbers if not show.seat(n).is_takeable(now))
            if taken:
                raise SeatUnavailableError(
                    f"show {show.id}: seats already taken: {', '.join(taken)}"
                )
            expires_at = now + self._ttl
            for number in seat_numbers:
                show.seat(number).hold(booking_id, expires_at)
            return expires_at

    def confirm(self, show: Show, seat_numbers: Sequence[str], booking_id: str) -> None:
        """HELD -> BOOKED. Raises if the sweeper reclaimed the hold while we were paying."""
        with self.seats_locked(show.id, seat_numbers):
            for number in seat_numbers:
                seat = show.seat(number)
                if seat.status is not SeatStatus.HELD or seat.held_by != booking_id:
                    raise HoldExpiredError(
                        f"hold on seat {number} for booking {booking_id} is gone "
                        f"(seat is {seat.status})"
                    )
            for number in seat_numbers:
                show.seat(number).book(booking_id)

    def release(self, show: Show, seat_numbers: Sequence[str], booking_id: str) -> int:
        """Give back seats this booking owns, held or booked. Idempotent."""
        with self.seats_locked(show.id, seat_numbers):
            released = 0
            for number in seat_numbers:
                seat = show.seat(number)
                mine = seat.held_by == booking_id or seat.booking_id == booking_id
                if mine and seat.status is not SeatStatus.AVAILABLE:
                    seat.release()
                    released += 1
            return released

    def sweep(self, show: Show) -> list[str]:
        """Reclaim every seat whose hold TTL ran out. Returns the affected booking ids.

        The candidate scan is lock-free (a hint); the reclaim re-checks under the same
        seat locks ``confirm`` uses, which is why a sweep can never half-steal a booking
        that is in the middle of being confirmed.
        """
        now = self._clock.now()
        stale = sorted(
            {
                seat.held_by
                for seat in show.all_seats()
                if seat.status is SeatStatus.HELD
                and seat.held_by is not None
                and (seat.hold_expires_at or 0.0) <= now
            }
        )
        expired: list[str] = []
        for booking_id in stale:
            numbers = [s.number for s in show.all_seats() if s.held_by == booking_id]
            if not numbers:
                continue
            with self.seats_locked(show.id, numbers):
                checked = self._clock.now()
                reclaimed = False
                for number in numbers:
                    seat = show.seat(number)
                    if (
                        seat.status is SeatStatus.HELD
                        and seat.held_by == booking_id
                        and (seat.hold_expires_at or 0.0) <= checked
                    ):
                        seat.release()
                        reclaimed = True
                if reclaimed:
                    expired.append(booking_id)
        return expired


# --8<-- [end:seat_locks]


# --8<-- [start:booking_service]
class BookingService:
    """Facade for the whole flow: select -> hold -> pay -> confirm, plus cancel and expiry.

    ``_bookings_lock`` guards ``_bookings`` and ``_by_key``. Every booking state
    transition happens under it, so two threads cannot both confirm the same booking.
    """

    def __init__(
        self,
        catalog: Catalog,
        locks: SeatLockService,
        gateway: PaymentGateway | None = None,
        pricing: PricingStrategy | None = None,
        refunds: RefundPolicy | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        payment_ids: IdGenerator | None = None,
    ) -> None:
        self._catalog = catalog
        self._locks = locks
        self._gateway = gateway or AlwaysApprovesGateway()
        self._pricing = pricing or SeatTypePricing()
        self._refunds = refunds or TieredRefundPolicy()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("BK")
        self._payment_ids = payment_ids or SequentialIdGenerator("PAY")
        self._bookings: dict[str, Booking] = {}
        self._by_key: dict[str, Payment] = {}
        self._payments: dict[str, Payment] = {}
        self._listeners: list[BookingListener] = []
        self._bookings_lock = threading.Lock()

    def subscribe(self, listener: BookingListener) -> None:
        self._listeners.append(listener)

    def quote(self, show_id: str, seat_numbers: Sequence[str]) -> Money:
        show = self._catalog.show(show_id)
        numbers = validate_seat_request(show, tuple(seat_numbers))
        return self._total(show, numbers)

    def create_booking(self, show_id: str, seat_numbers: Sequence[str], user_id: str) -> Booking:
        """Validate, price, then take the all-or-nothing hold. Booking starts PENDING."""
        show = self._catalog.show(show_id)
        numbers = validate_seat_request(show, tuple(seat_numbers))
        booking_id = self._ids.next_id()
        amount = self._total(show, numbers)
        expires_at = self._locks.hold(show, numbers, booking_id)  # raises SeatUnavailableError
        booking = Booking(
            id=booking_id,
            show_id=show.id,
            user_id=user_id,
            seat_numbers=numbers,
            amount=amount,
            created_at=self._clock.now(),
            hold_expires_at=expires_at,
        )
        with self._bookings_lock:
            self._bookings[booking.id] = booking
        return booking

    def pay(self, booking_id: str, method: PaymentMethod, idempotency_key: str) -> Booking:
        """Charge, then confirm. Replaying the same key never charges twice.

        The key is reserved *before* the charge, so a duplicate callback that arrives
        while the first is still at the gateway is rejected instead of double charging.
        """
        with self._bookings_lock:
            replay = self._by_key.get(idempotency_key)
            if replay is not None:
                return self._replay(replay)
            booking = self._require(booking_id)
            if booking.status is not BookingStatus.PENDING:
                raise BookingStateError(f"booking {booking_id} is {booking.status}, not pending")
            payment = Payment(
                id=self._payment_ids.next_id(),
                booking_id=booking.id,
                amount=booking.amount,
                method=method,
                idempotency_key=idempotency_key,
            )
            self._by_key[idempotency_key] = payment
            self._payments[payment.id] = payment

        if not self._gateway.charge(payment.id, booking.amount, method):
            with self._bookings_lock:
                payment.status = PaymentStatus.FAILED
            raise PaymentDeclinedError(
                f"{method} payment of {booking.amount} declined for booking {booking.id}"
            )

        show = self._catalog.show(booking.show_id)
        try:
            self._locks.confirm(show, booking.seat_numbers, booking.id)
        except HoldExpiredError:
            self._gateway.refund(payment.id, booking.amount)
            with self._bookings_lock:
                payment.status = PaymentStatus.REFUNDED
                if booking.status is BookingStatus.PENDING:
                    booking.transition_to(BookingStatus.EXPIRED)
            self._emit("booking_expired", booking)
            raise

        with self._bookings_lock:
            payment.status = PaymentStatus.CAPTURED
            booking.payment_id = payment.id
            booking.transition_to(BookingStatus.CONFIRMED)
        self._emit("booking_confirmed", booking)
        return booking

    def cancel(self, booking_id: str) -> Money:
        """Release the seats and refund what the policy allows. Returns the refund."""
        now = self._clock.now()
        with self._bookings_lock:
            booking = self._require(booking_id)
            show = self._catalog.show(booking.show_id)
            refund = (
                self._refunds.refund(booking, show, now)
                if booking.status is BookingStatus.CONFIRMED
                else Money(0, booking.amount.currency)
            )
            booking.transition_to(BookingStatus.CANCELLED)
            booking.refunded = refund
            payment_id = booking.payment_id
        self._locks.release(show, booking.seat_numbers, booking.id)
        if payment_id is not None and not refund.is_zero():
            self._gateway.refund(payment_id, refund)
        self._emit("booking_cancelled", booking)
        return refund

    def expire_stale_holds(self) -> list[str]:
        """The sweeper body: reclaim timed-out seats and mark their bookings EXPIRED."""
        expired: list[Booking] = []
        for show in self._catalog.shows():
            for booking_id in self._locks.sweep(show):
                with self._bookings_lock:
                    booking = self._bookings.get(booking_id)
                    if booking is not None and booking.status is BookingStatus.PENDING:
                        booking.transition_to(BookingStatus.EXPIRED)
                        expired.append(booking)
        for booking in expired:
            self._emit("booking_expired", booking)
        return [b.id for b in expired]

    def booking(self, booking_id: str) -> Booking:
        with self._bookings_lock:
            return self._require(booking_id)

    def _require(self, booking_id: str) -> Booking:
        try:
            return self._bookings[booking_id]
        except KeyError:
            raise BookingStateError(f"unknown booking {booking_id!r}") from None

    def _replay(self, payment: Payment) -> Booking:
        if payment.status is PaymentStatus.IN_FLIGHT:
            raise PaymentInFlightError(f"payment {payment.id} is still in flight; do not retry yet")
        if payment.status is PaymentStatus.FAILED:
            raise PaymentDeclinedError(f"payment {payment.id} was declined; use a new key to retry")
        return self._require(payment.booking_id)

    def _total(self, show: Show, seat_numbers: Sequence[str]) -> Money:
        total = Money(0, show.base_price.currency)
        for number in seat_numbers:
            total = total + self._pricing.price(show, show.seat(number))
        return total

    def _emit(self, event: str, booking: Booking) -> None:
        # Outside the lock: a slow listener must never stall a paying user.
        for listener in self._listeners:
            listener.on_booking_event(event, booking)


# --8<-- [end:booking_service]


# --8<-- [start:sweeper]
class HoldExpirySweeper:
    """Background eager expiry. Lazy expiry (``ShowSeat.is_takeable``) is the safety net.

    Without the sweeper a seat map would show sold-out until somebody tried to book;
    without lazy expiry a paused sweeper would leak inventory. You want both.
    """

    def __init__(self, bookings: BookingService, interval_seconds: float = 30.0) -> None:
        self._bookings = bookings
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[str]:
        return self._bookings.expire_stale_holds()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="hold-sweeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.run_once()


# --8<-- [end:sweeper]
