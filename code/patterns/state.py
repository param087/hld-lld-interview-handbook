"""State: behaviour that changes with an object's lifecycle, with every transition explicit.

The running example is a vending machine. ``VendingMachine`` (the Context) owns
the data (balance, stock, the selected slot) and hands every event to its current
``MachineState``; ``Idle``, ``HasMoney``, ``Dispensing`` and ``OutOfService``
accept the events that make sense for them, reject the rest, and move the machine
on by calling ``transition_to``. The last section restates the same lifecycle as an
``Enum`` plus a transition table, the Pythonic form when the per-state behaviour
is thin.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from common import (
    ConflictError,
    HandbookError,
    InvalidStateError,
    Money,
    NotFoundError,
    ValidationError,
)


# --8<-- [start:states]
class MachineState:
    """Base state: every event is rejected here; a subclass overrides the ones it accepts.

    States hold no data of their own (balance, stock and the selection live on the
    machine), so a fresh instance is created at each transition and nothing needs
    resetting. Each handler receives the machine because it is the machine's data
    it works on, and it is the machine's ``transition_to`` that moves things along.
    """

    @property
    def name(self) -> str:
        return type(self).__name__

    def _reject(self, event: str) -> InvalidStateError:
        return InvalidStateError(f"cannot {event} while {self.name}")

    def insert_money(self, machine: VendingMachine, amount: Money) -> None:
        raise self._reject("insert money")

    def select(self, machine: VendingMachine, code: str) -> None:
        raise self._reject("select")

    def dispense(self, machine: VendingMachine) -> tuple[str, Money]:
        raise self._reject("dispense")

    def cancel(self, machine: VendingMachine) -> Money:
        raise self._reject("cancel")

    def disable(self, machine: VendingMachine) -> Money:
        raise self._reject("disable")

    def enable(self, machine: VendingMachine) -> None:
        raise self._reject("enable")


class Idle(MachineState):
    """Nothing inserted: accepts money, or the operator taking the machine offline."""

    def insert_money(self, machine: VendingMachine, amount: Money) -> None:
        machine.add_balance(amount)
        machine.transition_to(HasMoney())

    def disable(self, machine: VendingMachine) -> Money:
        machine.transition_to(OutOfService())
        return Money(0)


class HasMoney(MachineState):
    """Credit on the display: more coins, a selection, or a refund."""

    def insert_money(self, machine: VendingMachine, amount: Money) -> None:
        machine.add_balance(amount)  # a self-transition: the state object stays

    def select(self, machine: VendingMachine, code: str) -> None:
        machine.reserve(code)  # raises on a bad code, an empty slot or a short balance: no transition
        machine.transition_to(Dispensing())

    def cancel(self, machine: VendingMachine) -> Money:
        refund = machine.refund()
        machine.transition_to(Idle())
        return refund


class Dispensing(MachineState):
    """The motor is running: only the hardware's confirmation or a fault moves us on."""

    def dispense(self, machine: VendingMachine) -> tuple[str, Money]:
        product, change = machine.release()
        machine.transition_to(Idle())
        return product, change

    def disable(self, machine: VendingMachine) -> Money:
        refund = machine.refund()  # a jam: give the money back, then take the machine offline
        machine.transition_to(OutOfService())
        return refund


class OutOfService(MachineState):
    """Offline: accepts nothing except the operator bringing it back."""

    def enable(self, machine: VendingMachine) -> None:
        machine.transition_to(Idle())


# --8<-- [end:states]


# --8<-- [start:context]
@dataclass(slots=True)
class Slot:
    """One product tray: the data the states operate on."""

    product: str
    price: Money
    quantity: int


class VendingMachine:
    """The Context: owns the data and the current state, and delegates every event to it.

    ``_lock`` guards ``_state``, ``_balance``, ``_selected`` and the slots. An event
    is check-and-transition inside one critical section, so two threads racing to
    ``select`` cannot both reach ``Dispensing``. The helpers below the events are
    called by states from inside an event, with the lock already held, and do not
    take it again.
    """

    def __init__(self, slots: Mapping[str, Slot]) -> None:
        self._slots = dict(slots)
        self._state: MachineState = Idle()
        self._balance = Money(0)
        self._selected: str | None = None
        self._lock = threading.Lock()
        self.transitions: list[tuple[str, str]] = []

    @property
    def state_name(self) -> str:
        with self._lock:
            return self._state.name

    @property
    def balance(self) -> Money:
        with self._lock:
            return self._balance

    def quantity(self, code: str) -> int:
        with self._lock:
            return self._slots[code].quantity

    # -- events: one delegation each, under the lock ----------------------------------
    def insert_money(self, amount: Money) -> None:
        if amount.cents <= 0:
            raise ValidationError("insert a positive amount")
        with self._lock:
            self._state.insert_money(self, amount)

    def select(self, code: str) -> None:
        with self._lock:
            self._state.select(self, code)

    def dispense(self) -> tuple[str, Money]:
        with self._lock:
            return self._state.dispense(self)

    def cancel(self) -> Money:
        with self._lock:
            return self._state.cancel(self)

    def disable(self) -> Money:
        with self._lock:
            return self._state.disable(self)

    def enable(self) -> None:
        with self._lock:
            self._state.enable(self)

    # -- called by states, lock already held -------------------------------------------
    def transition_to(self, state: MachineState) -> None:
        self.transitions.append((self._state.name, state.name))
        self._state = state

    def add_balance(self, amount: Money) -> None:
        self._balance += amount

    def reserve(self, code: str) -> None:
        slot = self._slots.get(code)
        if slot is None:
            raise NotFoundError(f"no slot {code}")
        if slot.quantity == 0:
            raise ConflictError(f"{slot.product} is sold out")
        if self._balance < slot.price:
            raise ValidationError(f"insert {slot.price - self._balance} more for {slot.product}")
        self._selected = code

    def release(self) -> tuple[str, Money]:
        if self._selected is None:
            raise InvalidStateError("nothing was selected")
        slot = self._slots[self._selected]
        slot.quantity -= 1
        change = self._balance - slot.price
        self._balance, self._selected = Money(0), None
        return slot.product, change

    def refund(self) -> Money:
        refund, self._balance, self._selected = self._balance, Money(0), None
        return refund


# --8<-- [end:context]


# --8<-- [start:table]
class Status(StrEnum):
    IDLE = "idle"
    HAS_MONEY = "has_money"
    DISPENSING = "dispensing"
    OUT_OF_SERVICE = "out_of_service"


class Event(StrEnum):
    INSERT = "insert"
    SELECT = "select"
    DISPENSE = "dispense"
    CANCEL = "cancel"
    DISABLE = "disable"
    ENABLE = "enable"


# The whole lifecycle on one screen; a missing key *is* the rejection.
TRANSITIONS: Final[Mapping[tuple[Status, Event], Status]] = MappingProxyType(
    {
        (Status.IDLE, Event.INSERT): Status.HAS_MONEY,
        (Status.IDLE, Event.DISABLE): Status.OUT_OF_SERVICE,
        (Status.HAS_MONEY, Event.INSERT): Status.HAS_MONEY,
        (Status.HAS_MONEY, Event.SELECT): Status.DISPENSING,
        (Status.HAS_MONEY, Event.CANCEL): Status.IDLE,
        (Status.DISPENSING, Event.DISPENSE): Status.IDLE,
        (Status.DISPENSING, Event.DISABLE): Status.OUT_OF_SERVICE,
        (Status.OUT_OF_SERVICE, Event.ENABLE): Status.IDLE,
    }
)


def next_status(status: Status, event: Event) -> Status:
    """A pure function: the table decides, the caller keeps the data."""
    try:
        return TRANSITIONS[(status, event)]
    except KeyError:
        raise InvalidStateError(f"cannot {event} while {status}") from None


def next_status_guarded(status: Status, event: Event, *, balance: Money, price: Money) -> Status:
    """``match`` for the one transition that carries a guard; the rest still comes from the table."""
    match status, event:
        case Status.HAS_MONEY, Event.SELECT if balance < price:
            raise ValidationError(f"insert {price - balance} more")
        case _:
            return next_status(status, event)


# --8<-- [end:table]


def main() -> None:
    machine = VendingMachine(
        {
            "A1": Slot("cola", Money.of("1.50"), quantity=2),
            "B2": Slot("chips", Money.of("1.00"), quantity=0),
        }
    )

    def show(label: str, outcome: str) -> None:
        print(f"{label:<24} {outcome}")

    def rejected(label: str, action: Callable[[], object]) -> None:
        try:
            action()
        except HandbookError as exc:
            show(label, f"rejected: {exc} (still {machine.state_name})")

    print("--- happy path: coins in, a selection, the motor confirms ---")
    for _ in range(2):
        machine.insert_money(Money.of("1.00"))
        show("insert 1.00 USD", f"-> {machine.state_name}, balance {machine.balance}")
    machine.select("A1")
    show("select A1 (cola 1.50)", f"-> {machine.state_name}")
    product, change = machine.dispense()
    show("dispense", f"-> {machine.state_name}, tray: {product}, change {change}")

    print("--- every other event is refused by the state, not by an if-ladder in the machine ---")
    rejected("select A1", lambda: machine.select("A1"))
    rejected("dispense", machine.dispense)
    machine.insert_money(Money.of("0.50"))
    show("insert 0.50 USD", f"-> {machine.state_name}, balance {machine.balance}")
    rejected("select B2 (sold out)", lambda: machine.select("B2"))
    rejected("select A1 (cola 1.50)", lambda: machine.select("A1"))
    refund = machine.cancel()
    show("cancel", f"-> {machine.state_name}, refunded {refund}")

    print("--- maintenance: only an idle machine can be taken offline ---")
    machine.disable()
    show("disable", f"-> {machine.state_name}")
    rejected("insert 1.00 USD", lambda: machine.insert_money(Money.of("1.00")))
    machine.enable()
    show("enable", f"-> {machine.state_name}")
    path = [machine.transitions[0][0], *(to for _, to in machine.transitions)]
    print("transitions: " + " -> ".join(path))

    print("--- the same lifecycle as an Enum and a transition table ---")
    status = Status.IDLE
    for event in (Event.INSERT, Event.SELECT, Event.DISPENSE):
        before, status = status, next_status(status, event)
        print(f"{before} + {event} -> {status}")
    try:
        next_status(status, Event.DISPENSE)
    except InvalidStateError as exc:
        print(f"{status} + {Event.DISPENSE} -> rejected: {exc}")
    try:
        next_status_guarded(Status.HAS_MONEY, Event.SELECT, balance=Money.of("0.50"), price=Money.of("1.50"))
    except ValidationError as exc:
        print(f"has_money + select with 0.50 USD against 1.50 USD -> rejected: {exc}")


if __name__ == "__main__":
    main()
