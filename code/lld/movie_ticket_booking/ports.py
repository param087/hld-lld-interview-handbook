"""The two boundaries to the outside world: the payment gateway and the notifiers.

Both are Protocols with an in-memory double, so tests never touch a network and the
booking service never learns what an email is.
"""

from __future__ import annotations

import threading
from typing import Protocol

from common import Money
from lld.movie_ticket_booking.models import Booking, PaymentMethod


# --8<-- [start:collaborators]
class PaymentGateway(Protocol):
    """The external processor. Charge is slow, so it is never called under a lock."""

    def charge(self, payment_id: str, amount: Money, method: PaymentMethod) -> bool: ...

    def refund(self, payment_id: str, amount: Money) -> None: ...


class AlwaysApprovesGateway:
    """Test double standing in for the card network."""

    def __init__(self) -> None:
        self.refunds: list[tuple[str, Money]] = []

    def charge(self, payment_id: str, amount: Money, method: PaymentMethod) -> bool:
        return True

    def refund(self, payment_id: str, amount: Money) -> None:
        self.refunds.append((payment_id, amount))


class BookingListener(Protocol):
    """Observer interface. The booking service pushes; listeners never poll."""

    def on_booking_event(self, event: str, booking: Booking) -> None: ...


class NotificationService:
    """Collects what would be an email, an SMS and a seat-map push in production."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outbox: list[str] = []

    def on_booking_event(self, event: str, booking: Booking) -> None:
        seats = ",".join(booking.seat_numbers)
        with self._lock:
            self._outbox.append(f"{event}: {booking.id} seats {seats} ({booking.amount})")

    def outbox(self) -> list[str]:
        with self._lock:
            return list(self._outbox)


# --8<-- [end:collaborators]
