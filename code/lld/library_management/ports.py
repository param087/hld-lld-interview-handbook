"""The notification boundary: told when a hold becomes collectable or lapses."""

from __future__ import annotations

import threading
from typing import Protocol

from lld.library_management.models import Book, Reservation


# --8<-- [start:observer]
class HoldListener(Protocol):
    """Observer interface: told when a hold becomes collectable or lapses."""

    def on_hold_event(self, event: str, reservation: Reservation, book: Book) -> None: ...


class NotificationService:
    """Stands in for the email and SMS providers. ``_lock`` guards the outbox."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outbox: list[str] = []

    def on_hold_event(self, event: str, reservation: Reservation, book: Book) -> None:
        with self._lock:
            self._outbox.append(
                f"{event}: {book.title} for {reservation.account_id} "
                f"(copy {reservation.barcode}, by {reservation.pickup_by})"
            )

    def outbox(self) -> list[str]:
        with self._lock:
            return list(self._outbox)


# --8<-- [end:observer]
