"""Order edits as Command objects, so "undo that" is one method instead of a rewrite.

The pattern earns its place here because of a real rule: while the order is OPEN an
edit can simply be undone, but once the ticket is SENT the kitchen already has the
docket, so the only legal edit is a *void* - which is itself a command, and is the one
the manager has to authorise.
"""

from __future__ import annotations

from typing import Protocol

from common import Money
from lld.restaurant_management.models import (
    Modifier,
    Order,
    OrderItem,
    OrderStateError,
    OrderStatus,
    Staff,
    StaffRole,
)


# --8<-- [start:commands]
class OrderCommand(Protocol):
    """Every edit is an object: it can be applied, undone and written to an audit log."""

    def apply(self, order: Order) -> None: ...

    def undo(self, order: Order) -> None: ...

    def describe(self) -> str: ...


class AddItem:
    def __init__(self, item: OrderItem) -> None:
        self._item = item

    def apply(self, order: Order) -> None:
        if order.status is not OrderStatus.OPEN:
            raise OrderStateError(f"order {order.id} is {order.status}; send a new course instead")
        order.items.append(self._item)

    def undo(self, order: Order) -> None:
        order.items = [i for i in order.items if i.id != self._item.id]

    def describe(self) -> str:
        return f"add {self._item.quantity} x {self._item.name}"


class ChangeQuantity:
    def __init__(self, item_id: str, quantity: int) -> None:
        self._item_id = item_id
        self._quantity = quantity
        self._previous: int | None = None

    def apply(self, order: Order) -> None:
        if order.status is not OrderStatus.OPEN:
            raise OrderStateError(f"order {order.id} is {order.status}; quantities are fixed")
        item = order.item(self._item_id)
        self._previous = item.quantity
        item.quantity = self._quantity

    def undo(self, order: Order) -> None:
        if self._previous is not None:
            order.item(self._item_id).quantity = self._previous

    def describe(self) -> str:
        return f"set line {self._item_id} to {self._quantity}"


class AddModifier:
    def __init__(self, item_id: str, modifier: Modifier) -> None:
        self._item_id = item_id
        self._modifier = modifier

    def apply(self, order: Order) -> None:
        if order.status is not OrderStatus.OPEN:
            raise OrderStateError(f"order {order.id} is {order.status}; modifiers are fixed")
        item = order.item(self._item_id)
        item.modifiers = (*item.modifiers, self._modifier)

    def undo(self, order: Order) -> None:
        item = order.item(self._item_id)
        item.modifiers = tuple(m for m in item.modifiers if m != self._modifier)

    def describe(self) -> str:
        return f"add {self._modifier.name} to {self._item_id}"


class VoidItem:
    """The only edit allowed after the ticket is sent, and it needs a manager."""

    def __init__(self, item_id: str, reason: str, authorised_by: Staff) -> None:
        if authorised_by.role is not StaffRole.MANAGER:
            raise OrderStateError(f"{authorised_by.role} cannot void a sent line")
        self._item_id = item_id
        self.reason = reason
        self.authorised_by = authorised_by

    def apply(self, order: Order) -> None:
        order.item(self._item_id).voided = True

    def undo(self, order: Order) -> None:
        order.item(self._item_id).voided = False

    def describe(self) -> str:
        return f"void {self._item_id} ({self.reason}) by {self.authorised_by.name}"


def line_price(name: str, unit_price: Money) -> str:
    """Small helper used by the kitchen docket renderer."""
    return f"{name} @ {unit_price}"


# --8<-- [end:commands]
