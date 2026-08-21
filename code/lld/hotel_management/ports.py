"""The boundaries to the outside world: the payment gateway, notifications, housekeeping.

Each is a Protocol with an in-memory implementation, so the front desk never learns what
an email provider or a cleaning rota is.
"""

from __future__ import annotations

import threading
from typing import Protocol

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock, ValidationError
from lld.hotel_management.models import (
    HousekeepingTask,
    PaymentMethod,
    Reservation,
    Room,
    Staff,
    StaffRole,
    TaskKind,
)


# --8<-- [start:collaborators]
class PaymentGateway(Protocol):
    def charge(self, payment_id: str, amount: Money, method: PaymentMethod) -> bool: ...

    def refund(self, payment_id: str, amount: Money) -> None: ...


class AlwaysApprovesGateway:
    def __init__(self) -> None:
        self.refunds: list[tuple[str, Money]] = []

    def charge(self, payment_id: str, amount: Money, method: PaymentMethod) -> bool:
        return True

    def refund(self, payment_id: str, amount: Money) -> None:
        self.refunds.append((payment_id, amount))


class StayListener(Protocol):
    """Observer interface: the front desk pushes stay events, listeners never poll."""

    def on_stay_event(self, event: str, reservation: Reservation, room: Room | None) -> None: ...


class NotificationService:
    """Booking confirmations and reminders; in production an email or SMS provider."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outbox: list[str] = []

    def on_stay_event(self, event: str, reservation: Reservation, room: Room | None) -> None:
        where = f" room {room.number}" if room is not None else ""
        with self._lock:
            self._outbox.append(f"{event}: {reservation.id}{where} ({reservation.stay})")

    def outbox(self) -> list[str]:
        with self._lock:
            return list(self._outbox)


class HousekeepingService:
    """Observer that turns a checkout into work. ``_lock`` guards the task board."""

    def __init__(self, clock: Clock | None = None, ids: IdGenerator | None = None) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("HK")
        self._tasks: dict[str, HousekeepingTask] = {}
        self._lock = threading.Lock()

    def on_stay_event(self, event: str, reservation: Reservation, room: Room | None) -> None:
        if event == "checked_out" and room is not None:
            kind = TaskKind.DEEP_CLEAN if reservation.stay.nights_count >= 5 else TaskKind.TURNDOWN
            self.create_task(room.number, kind)

    def create_task(self, room_number: str, kind: TaskKind) -> HousekeepingTask:
        """Factory for the task board: the kind decides the checklist, not the caller."""
        task = HousekeepingTask(
            id=self._ids.next_id(), room_number=room_number, kind=kind, created_at=self._clock.now()
        )
        with self._lock:
            self._tasks[task.id] = task
        return task

    def assign(self, task_id: str, staff: Staff) -> HousekeepingTask:
        if staff.role is not StaffRole.HOUSEKEEPER:
            raise ValidationError(f"{staff.role} cannot take housekeeping tasks")
        with self._lock:
            task = self._tasks[task_id]
            task.assigned_to = staff.id
            return task

    def complete(self, task_id: str) -> HousekeepingTask:
        with self._lock:
            task = self._tasks[task_id]
            task.done = True
            return task

    def open_tasks(self) -> list[HousekeepingTask]:
        with self._lock:
            return sorted((t for t in self._tasks.values() if not t.done), key=lambda t: t.id)


# --8<-- [end:collaborators]
