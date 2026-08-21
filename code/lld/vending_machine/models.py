"""Denominations, slots, recipes, transactions and the domain errors.

The three protocols at the bottom are the seams the machine is built on: where
items come from (`ItemSource`), what physically hands them over (`Dispenser`),
and who wants to hear that something is running out (`StockListener`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Protocol

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class Coin(IntEnum):
    """What the machine accepts *and* pays out, in cents."""

    FIVE = 5
    TEN = 10
    QUARTER = 25
    HALF = 50
    DOLLAR = 100

    @property
    def money(self) -> Money:
        return Money(int(self))


class Note(IntEnum):
    """What the machine accepts but never pays out: change comes back in coins."""

    ONE = 100
    FIVE = 500
    TEN = 1000

    @property
    def money(self) -> Money:
        return Money(int(self))


class Ingredient(StrEnum):
    WATER = "water"
    BEANS = "beans"
    MILK = "milk"
    SUGAR = "sugar"
    CHOCOLATE = "chocolate"


# --8<-- [end:enums]


# --8<-- [start:errors]
class UnknownSlotError(NotFoundError):
    """No such selection code on this machine."""


class OutOfStockError(ConflictError):
    """The slot exists but is empty."""


class OutOfIngredientError(ConflictError):
    """The coffee machine cannot brew this recipe with what is left in the pantry."""


class InsufficientFundsError(ValidationError):
    """The balance does not cover the price; the message says how much is missing."""


class InsufficientChangeError(ConflictError):
    """The cash box cannot make the change this purchase would require."""


class IllegalActionError(InvalidStateError):
    """The event is not legal in the machine's current state."""


class DispenseFailedError(ConflictError):
    """The motor did not hand the item over; the money is refunded and the stock restored."""


class SlotFullError(ValidationError):
    """A restock would put more items in the slot than it can hold."""


# --8<-- [end:errors]


# --8<-- [start:entities]
@dataclass(frozen=True, slots=True)
class Product:
    code: str
    name: str
    price: Money


@dataclass(slots=True)
class Slot:
    """One column of the machine: a product, how many are left, how many fit."""

    code: str
    product: Product
    quantity: int
    capacity: int = 10

    def is_empty(self) -> bool:
        return self.quantity == 0

    def take(self) -> str:
        if self.is_empty():
            raise OutOfStockError(f"{self.product.name} is sold out")
        self.quantity -= 1
        return self.product.name

    def put_back(self) -> None:
        self.quantity = min(self.capacity, self.quantity + 1)

    def restock(self, count: int) -> None:
        if count < 1:
            raise ValidationError("restock count must be positive")
        if self.quantity + count > self.capacity:
            raise SlotFullError(f"slot {self.code} holds {self.capacity}, has {self.quantity}")
        self.quantity += count


@dataclass(slots=True)
class CashBox:
    """The float plus everything customers have paid in. Coins are payable, notes are not."""

    coins: dict[Coin, int] = field(default_factory=dict)
    notes: dict[Note, int] = field(default_factory=dict)

    def add(self, denomination: Coin | Note) -> None:
        target = self.coins if isinstance(denomination, Coin) else self.notes
        target[denomination] = target.get(denomination, 0) + 1  # type: ignore[index]

    def take(self, coins: Iterable[Coin]) -> None:
        for coin in coins:
            remaining = self.coins.get(coin, 0)
            if remaining < 1:
                raise InsufficientChangeError(f"no {coin.money} coin left in the box")
            self.coins[coin] = remaining - 1

    def payable(self) -> dict[Coin, int]:
        """A snapshot of the coins available for change (notes are not payable)."""
        return {coin: count for coin, count in self.coins.items() if count > 0}

    def total(self) -> Money:
        coins = sum(int(coin) * count for coin, count in self.coins.items())
        notes = sum(int(note) * count for note, count in self.notes.items())
        return Money(coins + notes)


@dataclass(frozen=True, slots=True)
class Transaction:
    """One completed purchase, kept for the audit trail and the tests."""

    id: str
    code: str
    item: str
    price: Money
    paid: Money
    change: tuple[Coin, ...]
    at: float

    def change_amount(self) -> Money:
        return Money(sum(int(coin) for coin in self.change))


@dataclass(frozen=True, slots=True)
class Reservation:
    """What the machine is holding for the customer between `select` and `dispense`."""

    code: str
    item: str
    price: Money


@dataclass(frozen=True, slots=True)
class Recipe:
    """What a drink costs the pantry: millilitres of water and milk, grams of the rest."""

    name: str
    price: Money
    amounts: Mapping[Ingredient, int] = field(default_factory=dict)


# --8<-- [end:entities]


# --8<-- [start:protocols]
class ItemSource(Protocol):
    """Where a selection code turns into something to hand over.

    A shelf of slots and a coffee bar of recipes both satisfy it, which is why the
    coffee machine is the same machine with a different source injected.
    """

    def price_of(self, code: str) -> Money: ...

    def reserve(self, code: str) -> str: ...

    def restore(self, code: str) -> None: ...


class Dispenser(Protocol):
    """The motor. Returns False when the item jams instead of dropping."""

    def eject(self, item: str) -> bool: ...


class StockListener(Protocol):
    """Observer: told when a slot or an ingredient crosses its low-stock threshold."""

    def on_low_stock(self, item: str, remaining: int) -> None: ...


# --8<-- [end:protocols]
