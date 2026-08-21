"""The shelf, the pantry, the coffee bar and the machine that drives all of them.

Two locks live here. `Inventory._lock` (and `IngredientInventory._lock`) protect
stock counts, so an operator restocking column B never blocks a customer buying
from column A. `VendingMachine._lock` serialises one customer session: state,
balance and reservation move together or not at all. The order is always machine
lock first, stock lock second - stock objects never call back into the machine.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock
from lld.vending_machine.beverages import Beverage
from lld.vending_machine.models import (
    CashBox,
    Coin,
    DispenseFailedError,
    Dispenser,
    IllegalActionError,
    Ingredient,
    InsufficientFundsError,
    ItemSource,
    Note,
    OutOfIngredientError,
    Reservation,
    Slot,
    StockListener,
    Transaction,
    UnknownSlotError,
)
from lld.vending_machine.states import Idle, MachineState
from lld.vending_machine.strategies import ChangeMaker, MinimalChangeMaker


# --8<-- [start:observer]
class MaintenanceLog:
    """Observer: the list the operator reads on the next visit."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: list[str] = []

    def on_low_stock(self, item: str, remaining: int) -> None:
        with self._lock:
            self._lines.append(f"low stock: {item} down to {remaining}")

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)


class MotorDispenser:
    """The real motor always works. Tests inject one that jams."""

    def eject(self, item: str) -> bool:
        return True


# --8<-- [end:observer]


# --8<-- [start:inventory]
class Inventory:
    """The shelf: slots behind their own lock, plus low-stock alerts.

    It satisfies `ItemSource`, which is all the machine knows about it.
    """

    def __init__(self, slots: Iterable[Slot], low_stock_at: int = 1) -> None:
        self._slots = {slot.code: slot for slot in slots}
        self._low_at = low_stock_at
        self._lock = threading.Lock()
        self._listeners: list[StockListener] = []

    def subscribe(self, listener: StockListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def price_of(self, code: str) -> Money:
        with self._lock:
            return self._slot(code).product.price

    def reserve(self, code: str) -> str:
        with self._lock:
            slot = self._slot(code)
            name = slot.take()  # raises OutOfStockError while the lock is held
            remaining = slot.quantity
        if remaining <= self._low_at:
            self._notify(f"{name} ({code})", remaining)
        return name

    def restore(self, code: str) -> None:
        with self._lock:
            self._slot(code).put_back()

    def restock(self, code: str, count: int) -> None:
        """Operator refill. Takes only this lock, so a customer elsewhere is unaffected."""
        with self._lock:
            self._slot(code).restock(count)

    def stock(self, code: str) -> int:
        with self._lock:
            return self._slot(code).quantity

    def codes(self) -> list[str]:
        with self._lock:
            return sorted(self._slots)

    def _slot(self, code: str) -> Slot:
        try:
            return self._slots[code]
        except KeyError:
            raise UnknownSlotError(f"no slot {code!r} on this machine") from None

    def _notify(self, item: str, remaining: int) -> None:
        for listener in list(self._listeners):  # outside the stock lock
            listener.on_low_stock(item, remaining)


# --8<-- [end:inventory]


# --8<-- [start:coffee]
class IngredientInventory:
    """The pantry: levels in millilitres and grams, consumed all-or-nothing."""

    def __init__(self, levels: Mapping[Ingredient, int], low_level_at: int = 40) -> None:
        self._levels = dict(levels)
        self._low_at = low_level_at
        self._lock = threading.Lock()
        self._listeners: list[StockListener] = []

    def subscribe(self, listener: StockListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def level(self, ingredient: Ingredient) -> int:
        with self._lock:
            return self._levels.get(ingredient, 0)

    def consume(self, amounts: Mapping[Ingredient, int]) -> None:
        """Take every ingredient or none: a half-brewed drink is worse than a refusal."""
        with self._lock:
            short = [i for i, amount in amounts.items() if self._levels.get(i, 0) < amount]
            if short:
                names = ", ".join(sorted(i.value for i in short))
                raise OutOfIngredientError(f"not enough {names}")
            for ingredient, amount in amounts.items():
                self._levels[ingredient] -= amount
            low = [(i, self._levels[i]) for i in amounts if self._levels[i] <= self._low_at]
        for ingredient, remaining in low:
            self._notify(ingredient.value, remaining)

    def restore(self, amounts: Mapping[Ingredient, int]) -> None:
        with self._lock:
            for ingredient, amount in amounts.items():
                self._levels[ingredient] = self._levels.get(ingredient, 0) + amount

    def refill(self, ingredient: Ingredient, amount: int) -> None:
        self.restore({ingredient: amount})

    def _notify(self, item: str, remaining: int) -> None:
        for listener in list(self._listeners):
            listener.on_low_stock(item, remaining)


class CoffeeBar:
    """An `ItemSource` over recipes instead of slots: the coffee machine in one class.

    Every button is a `Beverage`, which may be a decorated one, so "latte with an
    extra shot" is a menu entry rather than a class.
    """

    def __init__(self, menu: Mapping[str, Beverage], pantry: IngredientInventory) -> None:
        self._menu = dict(menu)
        self._pantry = pantry

    def price_of(self, code: str) -> Money:
        return self._drink(code).price()

    def reserve(self, code: str) -> str:
        drink = self._drink(code)
        self._pantry.consume(drink.ingredients())
        return drink.name()

    def restore(self, code: str) -> None:
        self._pantry.restore(self._drink(code).ingredients())

    def codes(self) -> list[str]:
        return sorted(self._menu)

    def _drink(self, code: str) -> Beverage:
        try:
            return self._menu[code]
        except KeyError:
            raise UnknownSlotError(f"no drink {code!r} on this machine") from None


# --8<-- [end:coffee]


# --8<-- [start:machine]
class VendingMachine:
    """The context of the State pattern: it holds the data and serialises the events.

    Each public event is one delegation under `_lock`, so check-and-transition is
    atomic. The helpers below the events run with the lock already held and must
    not take it again.
    """

    def __init__(
        self,
        source: ItemSource,
        cash: CashBox | None = None,
        change_maker: ChangeMaker | None = None,
        dispenser: Dispenser | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._source = source
        self._cash = cash or CashBox()
        self._change = change_maker or MinimalChangeMaker()
        self._dispenser = dispenser or MotorDispenser()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("TX")
        self._state: MachineState = Idle()
        self._balance = Money(0)
        self._reserved: Reservation | None = None
        self._lock = threading.Lock()
        self.transitions: list[tuple[str, str]] = []

    # -- events: one line each, all of them guarded ---------------------------------
    def insert(self, denomination: Coin | Note) -> None:
        with self._lock:
            self._state.insert(self, denomination)

    def select(self, code: str) -> None:
        with self._lock:
            self._state.select(self, code)

    def dispense(self) -> Transaction:
        with self._lock:
            return self._state.dispense(self)

    def cancel(self) -> Money:
        with self._lock:
            return self._state.cancel(self)

    def take_offline(self) -> Money:
        with self._lock:
            return self._state.take_offline(self)

    def bring_online(self) -> None:
        with self._lock:
            self._state.bring_online(self)

    # -- operator -------------------------------------------------------------------
    def collect_cash(self, leave: Mapping[Coin, int] | None = None) -> Money:
        """Empty the box, optionally leaving a float of coins for change."""
        with self._lock:
            if not self._balance.is_zero():
                raise IllegalActionError("cannot collect while a customer has a balance")
            total = self._cash.total()
            self._cash.coins = dict(leave or {})
            self._cash.notes = {}
            return total - self._cash.total()

    # -- reads ----------------------------------------------------------------------
    def status(self) -> str:
        with self._lock:
            return self._state.name

    def balance(self) -> Money:
        with self._lock:
            return self._balance

    def cash_total(self) -> Money:
        with self._lock:
            return self._cash.total()

    def payable_coins(self) -> dict[Coin, int]:
        with self._lock:
            return self._cash.payable()

    # -- helpers: the states call these with `_lock` already held ---------------------
    def accept(self, denomination: Coin | Note) -> None:
        self._cash.add(denomination)
        self._balance = self._balance + denomination.money

    def reserve(self, code: str) -> None:
        """Validate everything that can fail, then hold the item. Order matters."""
        price = self._source.price_of(code)  # UnknownSlotError
        if self._balance < price:
            raise InsufficientFundsError(f"insert {price - self._balance} more for {code}")
        change_due = self._balance - price
        if not change_due.is_zero():
            self._change.plan(change_due, self._cash.payable())  # InsufficientChangeError
        item = self._source.reserve(code)  # OutOfStockError / OutOfIngredientError
        self._reserved = Reservation(code, item, price)

    def restore(self) -> None:
        if self._reserved is not None:
            self._source.restore(self._reserved.code)
            self._reserved = None

    def release(self) -> Transaction:
        """Eject first: nothing is charged and no change is paid until the item is out."""
        reserved = self._reserved
        if reserved is None:
            raise IllegalActionError("nothing is reserved")
        if not self._dispenser.eject(reserved.item):
            raise DispenseFailedError(f"{reserved.item} jammed on the way out")
        change_due = self._balance - reserved.price
        coins = () if change_due.is_zero() else self._change.plan(change_due, self._cash.payable())
        self._cash.take(coins)
        transaction = Transaction(
            id=self._ids.next_id(),
            code=reserved.code,
            item=reserved.item,
            price=reserved.price,
            paid=self._balance,
            change=coins,
            at=self._clock.now(),
        )
        self._balance = Money(0)
        self._reserved = None
        return transaction

    def refund(self) -> Money:
        """Give the balance back in coins. It can fail: the box may not break a note."""
        if self._balance.is_zero():
            return Money(0)
        coins = self._change.plan(self._balance, self._cash.payable())
        self._cash.take(coins)
        refunded = self._balance
        self._balance = Money(0)
        return refunded

    def transition_to(self, state: MachineState) -> None:
        self.transitions.append((self._state.name, state.name))
        self._state = state


# --8<-- [end:machine]
