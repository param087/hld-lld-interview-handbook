"""The floor plan, the kitchen board and the point-of-sale facade.

Two lock families, the same shape as the movie-ticket and hotel siblings:

* ``FloorPlan._locks`` holds **one lock per table**. Joining two tables for a party of
  eight acquires both, in sorted table-id order, so two hosts cannot deadlock.
* ``PointOfSale._orders_lock`` guards the order registry, the edit history, the
  reservation book and the waitlist. It is never held while a table lock is held.
* ``Kitchen._lock`` guards the ticket board only; listeners run outside it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Protocol

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.restaurant_management.models import (
    KitchenTicket,
    Order,
    OrderStateError,
    Table,
    TableStatus,
    TableUnavailableError,
    TicketStatus,
    UnknownItemError,
    validate_party,
)


# --8<-- [start:floor_plan]
class FloorPlan:
    """The tables and the lock that makes seating atomic *per table*.

    Two hosts seating two parties at different tables never contend; two hosts
    reaching for table 4 serialise on table 4 alone.
    """

    def __init__(self, tables: Sequence[Table]) -> None:
        self._tables: dict[str, Table] = {t.id: t for t in tables}
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def _lock_for(self, table_id: str) -> threading.Lock:
        with self._registry_lock:
            return self._locks.setdefault(table_id, threading.Lock())

    @contextmanager
    def tables_locked(self, table_ids: Sequence[str]) -> Iterator[None]:
        """Sorted acquisition: joining tables 7 and 4 always takes 4 first."""
        acquired: list[threading.Lock] = []
        try:
            for table_id in sorted(set(table_ids)):
                lock = self._lock_for(table_id)
                lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def table(self, table_id: str) -> Table:
        try:
            return self._tables[table_id]
        except KeyError:
            raise UnknownItemError(f"no table {table_id!r}") from None

    def tables(self) -> list[Table]:
        return sorted(self._tables.values(), key=lambda t: t.id)

    def suggest(self, party_size: int) -> tuple[str, ...] | None:
        """Lock-free hint: the smallest single free table, else the smallest free pair."""
        free = [t for t in self.tables() if t.status is TableStatus.AVAILABLE]
        fits = sorted((t for t in free if t.capacity >= party_size), key=lambda t: t.capacity)
        if fits:
            return (fits[0].id,)
        for i, first in enumerate(free):
            for second in free[i + 1 :]:
                if first.capacity + second.capacity >= party_size:
                    return tuple(sorted((first.id, second.id)))
        return None

    def seat(
        self,
        table_ids: Sequence[str],
        party_size: int,
        order_id: str,
        reservation_id: str | None = None,
    ) -> None:
        """All-or-nothing: check every table, check the seats, then occupy every table."""
        with self.tables_locked(table_ids):
            tables = [self.table(t) for t in table_ids]
            blocked = [
                t.id
                for t in tables
                if not (
                    t.status is TableStatus.AVAILABLE
                    or (t.status is TableStatus.RESERVED and t.reserved_for == reservation_id)
                )
            ]
            if blocked:
                raise TableUnavailableError(f"tables not free: {', '.join(sorted(blocked))}")
            validate_party(party_size, sum(t.capacity for t in tables))
            for table in tables:
                table.occupy(order_id)

    def hold_for_reservation(self, table_ids: Sequence[str], reservation_id: str) -> None:
        with self.tables_locked(table_ids):
            tables = [self.table(t) for t in table_ids]
            blocked = [t.id for t in tables if t.status is not TableStatus.AVAILABLE]
            if blocked:
                raise TableUnavailableError(f"cannot reserve: {', '.join(sorted(blocked))}")
            for table in tables:
                table.reserve(reservation_id)

    def clear(self, table_ids: Sequence[str]) -> None:
        with self.tables_locked(table_ids):
            for table_id in table_ids:
                self.table(table_id).start_cleaning()

    def mark_clean(self, table_id: str) -> None:
        with self.tables_locked([table_id]):
            self.table(table_id).mark_clean()

    def release(self, table_ids: Sequence[str]) -> None:
        with self.tables_locked(table_ids):
            for table_id in table_ids:
                self.table(table_id).release()


# --8<-- [end:floor_plan]


# --8<-- [start:kitchen]
class KitchenListener(Protocol):
    """Observer interface: the kitchen display and the waiter pager both implement it."""

    def on_ticket_event(self, event: str, ticket: KitchenTicket) -> None: ...


class KitchenDisplay:
    """The screen over the pass. It never polls; the kitchen pushes to it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._board: dict[str, KitchenTicket] = {}

    def on_ticket_event(self, event: str, ticket: KitchenTicket) -> None:
        with self._lock:
            self._board[ticket.id] = ticket

    def render(self) -> str:
        with self._lock:
            live = [t for t in self._board.values() if t.status is not TicketStatus.SERVED]
            return " | ".join(f"{t.id} {','.join(t.table_ids)} {t.status}" for t in sorted(live, key=lambda t: t.id))


class WaiterPager:
    """The other observer: it only cares about READY, and it buzzes the server."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pages: list[str] = []

    def on_ticket_event(self, event: str, ticket: KitchenTicket) -> None:
        if event == "ready":
            with self._lock:
                self._pages.append(f"pickup for {','.join(ticket.table_ids)} ({ticket.id})")

    def pages(self) -> list[str]:
        with self._lock:
            return list(self._pages)


class Kitchen:
    """The ticket board. ``_lock`` guards it; listeners are notified outside the lock."""

    NEXT_STATUS = {
        TicketStatus.QUEUED: TicketStatus.PREPARING,
        TicketStatus.PREPARING: TicketStatus.READY,
        TicketStatus.READY: TicketStatus.SERVED,
    }

    def __init__(self, clock: Clock | None = None, ids: IdGenerator | None = None) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("KT")
        self._tickets: dict[str, KitchenTicket] = {}
        self._listeners: list[KitchenListener] = []
        self._lock = threading.Lock()

    def subscribe(self, listener: KitchenListener) -> None:
        self._listeners.append(listener)

    def send(self, order: Order, lines: Sequence[str]) -> KitchenTicket:
        ticket = KitchenTicket(
            id=self._ids.next_id(),
            order_id=order.id,
            table_ids=order.table_ids,
            lines=tuple(lines),
            created_at=self._clock.now(),
        )
        with self._lock:
            self._tickets[ticket.id] = ticket
        self._notify("queued", ticket)
        return ticket

    def advance(self, ticket_id: str) -> KitchenTicket:
        """QUEUED to PREPARING to READY to SERVED, one step per call."""
        with self._lock:
            ticket = self._require(ticket_id)
            nxt = self.NEXT_STATUS.get(ticket.status)
            if nxt is None:
                raise OrderStateError(f"ticket {ticket_id} is already {ticket.status}")
            ticket.status = nxt
        self._notify(str(nxt), ticket)
        return ticket

    def void_line(self, ticket_id: str, description: str) -> KitchenTicket:
        """Tell the line that a dish is off. The docket keeps the strike-through."""
        with self._lock:
            ticket = self._require(ticket_id)
            ticket.lines = (*ticket.lines, f"VOID {description}")
        self._notify("voided", ticket)
        return ticket

    def board(self) -> list[KitchenTicket]:
        with self._lock:
            return sorted(self._tickets.values(), key=lambda t: t.id)

    def queue_depth(self) -> int:
        with self._lock:
            return sum(1 for t in self._tickets.values() if t.status is TicketStatus.QUEUED)

    def _require(self, ticket_id: str) -> KitchenTicket:
        try:
            return self._tickets[ticket_id]
        except KeyError:
            raise UnknownItemError(f"no kitchen ticket {ticket_id!r}") from None

    def _notify(self, event: str, ticket: KitchenTicket) -> None:
        for listener in self._listeners:
            listener.on_ticket_event(event, ticket)


# --8<-- [end:kitchen]
