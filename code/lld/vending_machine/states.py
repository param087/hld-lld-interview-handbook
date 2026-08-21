"""The four machine states (State pattern).

The base class rejects every event; each subclass overrides only the cells of the
state-event matrix it accepts, so the matrix is readable from the class bodies.
States hold no data - balance, selection and stock live on the machine - which is
why a fresh instance per transition is correct and cheap.

Every handler validates first and transitions last. A rejected selection leaves
the machine in `HasMoney` with the balance untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from common import Money
from lld.vending_machine.models import (
    Coin,
    DispenseFailedError,
    IllegalActionError,
    Note,
    Transaction,
)

if TYPE_CHECKING:
    from lld.vending_machine.services import VendingMachine


# --8<-- [start:states]
class MachineState:
    """Refuses everything. The refusal names the state, which is what tests assert on."""

    name = "MachineState"

    def insert(self, machine: VendingMachine, denomination: Coin | Note) -> None:
        raise IllegalActionError(f"cannot insert money while {self.name}")

    def select(self, machine: VendingMachine, code: str) -> None:
        raise IllegalActionError(f"cannot select while {self.name}")

    def dispense(self, machine: VendingMachine) -> Transaction:
        raise IllegalActionError(f"cannot dispense while {self.name}")

    def cancel(self, machine: VendingMachine) -> Money:
        raise IllegalActionError(f"nothing to cancel while {self.name}")

    def take_offline(self, machine: VendingMachine) -> Money:
        raise IllegalActionError(f"cannot go offline while {self.name}")

    def bring_online(self, machine: VendingMachine) -> None:
        raise IllegalActionError(f"cannot come online while {self.name}")


class Idle(MachineState):
    name = "Idle"

    def insert(self, machine: VendingMachine, denomination: Coin | Note) -> None:
        machine.accept(denomination)
        machine.transition_to(HasMoney())

    def take_offline(self, machine: VendingMachine) -> Money:
        machine.transition_to(OutOfService())
        return Money(0)


class HasMoney(MachineState):
    name = "HasMoney"

    def insert(self, machine: VendingMachine, denomination: Coin | Note) -> None:
        machine.accept(denomination)  # no transition: more money, same state

    def select(self, machine: VendingMachine, code: str) -> None:
        machine.reserve(code)  # raises on unknown code, empty slot, short balance, no change
        machine.transition_to(Dispensing())

    def cancel(self, machine: VendingMachine) -> Money:
        refund = machine.refund()
        machine.transition_to(Idle())
        return refund

    def take_offline(self, machine: VendingMachine) -> Money:
        refund = machine.refund()
        machine.transition_to(OutOfService())
        return refund


class Dispensing(MachineState):
    name = "Dispensing"

    def dispense(self, machine: VendingMachine) -> Transaction:
        try:
            transaction = machine.release()
        except DispenseFailedError:
            machine.restore()  # the item goes back on the shelf
            machine.refund()  # and the money goes back to the customer
            machine.transition_to(OutOfService())
            raise
        machine.transition_to(Idle())
        return transaction

    def take_offline(self, machine: VendingMachine) -> Money:
        machine.restore()
        refund = machine.refund()
        machine.transition_to(OutOfService())
        return refund


class OutOfService(MachineState):
    name = "OutOfService"

    def bring_online(self, machine: VendingMachine) -> None:
        machine.transition_to(Idle())


# --8<-- [end:states]
