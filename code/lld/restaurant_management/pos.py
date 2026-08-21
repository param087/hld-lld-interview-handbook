"""The Facade the terminal talks to: seat, order, send, serve, bill, pay, clear.

``_orders_lock`` guards the order registry, the per-order edit history, the reservation
book and the waitlist. Table locks live in ``FloorPlan`` and are never held at the same
time as this one, so the two families cannot form a cycle.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from decimal import Decimal

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock
from lld.restaurant_management.commands import AddItem, OrderCommand, VoidItem
from lld.restaurant_management.menu import MenuSection
from lld.restaurant_management.models import (
    Bill,
    BillLine,
    ItemUnavailableError,
    KitchenTicket,
    Modifier,
    Order,
    OrderItem,
    OrderStateError,
    OrderStatus,
    Payment,
    PaymentMethod,
    Reservation,
    Shift,
    Staff,
    TableUnavailableError,
    UnknownItemError,
    WaitlistEntry,
)
from lld.restaurant_management.services import FloorPlan, Kitchen
from lld.restaurant_management.strategies import (
    BillSplitStrategy,
    DiscountPolicy,
    NoDiscount,
    NoSplit,
)


# --8<-- [start:restaurant]
class Restaurant:
    """The aggregate the POS is built around: a floor, a menu, staff and a tax rate."""

    def __init__(
        self,
        name: str,
        floor: FloorPlan,
        menu: MenuSection,
        tax_rate: Decimal = Decimal("0.08"),
        staff: Sequence[Staff] = (),
    ) -> None:
        self.name = name
        self.floor = floor
        self.menu = menu
        self.tax_rate = tax_rate
        self.staff = tuple(staff)


# --8<-- [end:restaurant]


# --8<-- [start:pos]
class PointOfSale:
    """One object for the terminal to call. Everything else stays behind it."""

    def __init__(
        self,
        restaurant: Restaurant,
        kitchen: Kitchen,
        discount: DiscountPolicy | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._restaurant = restaurant
        self._floor = restaurant.floor
        self._kitchen = kitchen
        self._discount = discount or NoDiscount()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("ORD")
        self._orders: dict[str, Order] = {}
        self._history: dict[str, list[OrderCommand]] = {}
        self._tickets: dict[str, str] = {}  # order id -> latest ticket id
        self._reservations: dict[str, Reservation] = {}
        self._waitlist: list[WaitlistEntry] = []
        self._bills: dict[str, Bill] = {}
        self._payments: list[Payment] = []
        self._orders_lock = threading.Lock()

    # -- front of house ----------------------------------------------------------------
    def book(self, guest_name: str, party_size: int, slot_at: float, table_ids: Sequence[str]) -> Reservation:
        reservation = Reservation(
            id=self._ids.next_id(),
            guest_name=guest_name,
            party_size=party_size,
            slot_at=slot_at,
            table_ids=tuple(table_ids),
        )
        self._floor.hold_for_reservation(table_ids, reservation.id)
        with self._orders_lock:
            self._reservations[reservation.id] = reservation
        return reservation

    def join_waitlist(self, guest_name: str, party_size: int) -> WaitlistEntry:
        """Walk-ins queue in arrival order; the quote is the queue depth times a turn."""
        with self._orders_lock:
            entry = WaitlistEntry(
                id=self._ids.next_id(),
                guest_name=guest_name,
                party_size=party_size,
                joined_at=self._clock.now(),
                quoted_wait_minutes=15 * (len(self._waitlist) + 1),
            )
            self._waitlist.append(entry)
            return entry

    def seat_next_walk_in(self, server_id: str) -> tuple[WaitlistEntry, Order] | None:
        """FIFO, but skip a party that still does not fit rather than blocking the queue."""
        with self._orders_lock:
            candidates = list(self._waitlist)
        for entry in candidates:
            tables = self._floor.suggest(entry.party_size)
            if tables is None:
                continue
            try:
                order = self.seat_party(entry.party_size, server_id, tables)
            except TableUnavailableError:
                continue
            with self._orders_lock:
                self._waitlist = [e for e in self._waitlist if e.id != entry.id]
            return entry, order
        return None

    def seat_party(
        self,
        party_size: int,
        server_id: str,
        table_ids: Sequence[str] | None = None,
        reservation_id: str | None = None,
    ) -> Order:
        """Open a tab and occupy the tables atomically. The order id is minted first."""
        if table_ids is None:
            suggested = self._floor.suggest(party_size)
            if suggested is None:
                raise TableUnavailableError(f"nothing free for a party of {party_size}")
            table_ids = suggested
        order_id = self._ids.next_id()
        self._floor.seat(table_ids, party_size, order_id, reservation_id)
        order = Order(
            id=order_id,
            table_ids=tuple(sorted(table_ids)),
            server_id=server_id,
            opened_at=self._clock.now(),
        )
        with self._orders_lock:
            self._orders[order.id] = order
            self._history[order.id] = []
            if reservation_id is not None and reservation_id in self._reservations:
                self._reservations[reservation_id].seated = True
        return order

    # -- the tab -----------------------------------------------------------------------
    def add_item(
        self,
        order_id: str,
        component_id: str,
        quantity: int = 1,
        modifiers: tuple[Modifier, ...] = (),
    ) -> OrderItem:
        component = self._restaurant.menu.require(component_id)
        if not component.is_available():
            raise ItemUnavailableError(f"{component.name} is off the menu tonight")
        item = OrderItem(
            id=self._ids.next_id(),
            component_id=component.id,
            name=component.name,
            unit_price=component.price(),
            quantity=quantity,
            modifiers=modifiers,
        )
        self.apply_edit(order_id, AddItem(item))
        return item

    def apply_edit(self, order_id: str, command: OrderCommand) -> None:
        """Every edit goes through a Command, so the history is an audit log for free."""
        with self._orders_lock:
            order = self._require(order_id)
            command.apply(order)
            self._history[order_id].append(command)

    def undo_last_edit(self, order_id: str) -> str:
        with self._orders_lock:
            order = self._require(order_id)
            if order.status is not OrderStatus.OPEN:
                raise OrderStateError(f"order {order_id} is {order.status}; void the line instead")
            history = self._history[order_id]
            if not history:
                raise OrderStateError(f"order {order_id} has nothing to undo")
            command = history.pop()
            command.undo(order)
            return command.describe()

    def send_to_kitchen(self, order_id: str) -> KitchenTicket:
        with self._orders_lock:
            order = self._require(order_id)
            lines = [f"{i.quantity} x {i.name}" + self._modifier_suffix(i) for i in order.live_items()]
            if not lines:
                raise OrderStateError(f"order {order_id} has nothing to send")
            order.transition_to(OrderStatus.SENT)
        ticket = self._kitchen.send(order, lines)
        with self._orders_lock:
            self._tickets[order_id] = ticket.id
        return ticket

    def void_line(self, order_id: str, item_id: str, reason: str, manager: Staff) -> None:
        """The only edit allowed once the docket is on the pass, and it needs a manager."""
        with self._orders_lock:
            order = self._require(order_id)
            item = order.item(item_id)
            command = VoidItem(item_id, reason, manager)
            command.apply(order)
            self._history[order_id].append(command)
            ticket_id = self._tickets.get(order_id)
        if ticket_id is not None:
            self._kitchen.void_line(ticket_id, f"{item.quantity} x {item.name}")

    def next_course(self, order_id: str) -> None:
        """SENT back to OPEN so the dessert course joins the same tab."""
        with self._orders_lock:
            self._require(order_id).transition_to(OrderStatus.OPEN)

    def mark_served(self, order_id: str) -> None:
        with self._orders_lock:
            self._require(order_id).transition_to(OrderStatus.SERVED)

    # -- money -------------------------------------------------------------------------
    def bill(self, order_id: str, split: BillSplitStrategy | None = None) -> Bill:
        """Subtotal, discount, tax, then the split - in that order, always."""
        strategy = split or NoSplit()
        with self._orders_lock:
            order = self._require(order_id)
            subtotal = order.subtotal()
            discount = self._discount.discount(order, subtotal)
            taxable = subtotal - discount
            tax = taxable * self._restaurant.tax_rate
            total = taxable + tax
            lines = tuple(
                BillLine(f"{i.quantity} x {i.name}", i.line_total()) for i in order.live_items()
            )
            bill = Bill(
                id=self._ids.next_id(),
                order_id=order.id,
                lines=lines,
                subtotal=subtotal,
                discount=discount,
                tax=tax,
                total=total,
                shares=strategy.split(order, total),
            )
            order.transition_to(OrderStatus.BILLED)
            self._bills[bill.id] = bill
            return bill

    def pay(self, bill_id: str, method: PaymentMethod) -> Payment:
        with self._orders_lock:
            bill = self._bills.get(bill_id)
            if bill is None:
                raise UnknownItemError(f"no bill {bill_id!r}")
            order = self._require(bill.order_id)
            order.transition_to(OrderStatus.CLOSED)
            payment = Payment(
                id=self._ids.next_id(),
                bill_id=bill.id,
                amount=bill.total,
                method=method,
                paid_at=self._clock.now(),
            )
            self._payments.append(payment)
            return payment

    def clear_table(self, order_id: str) -> tuple[str, ...]:
        """OCCUPIED to CLEANING for every table on the tab. Bussers finish the job."""
        with self._orders_lock:
            order = self._require(order_id)
            if order.status is not OrderStatus.CLOSED:
                raise OrderStateError(f"order {order_id} is {order.status}, not paid")
            tables = order.table_ids
        self._floor.clear(tables)
        return tables

    # -- reporting ---------------------------------------------------------------------
    def daily_report(self, shift: Shift) -> dict[str, object]:
        with self._orders_lock:
            paid = [p for p in self._payments if shift.contains(p.paid_at)]
            revenue = Money(0)
            for payment in paid:
                revenue = revenue + payment.amount
            covers = sum(
                len(o.table_ids) for o in self._orders.values() if shift.contains(o.opened_at)
            )
            return {"shift": shift.name, "tabs": len(paid), "revenue": revenue, "tables_used": covers}

    def order(self, order_id: str) -> Order:
        with self._orders_lock:
            return self._require(order_id)

    def waitlist(self) -> list[WaitlistEntry]:
        with self._orders_lock:
            return list(self._waitlist)

    def _require(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise UnknownItemError(f"unknown order {order_id!r}") from None

    @staticmethod
    def _modifier_suffix(item: OrderItem) -> str:
        return f" ({', '.join(m.name for m in item.modifiers)})" if item.modifiers else ""


# --8<-- [end:pos]
