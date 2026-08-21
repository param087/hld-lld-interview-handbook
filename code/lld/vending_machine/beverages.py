"""The coffee variant: recipes as drinks, add-ons as decorators.

Three add-ons give seven combinations as subclasses and a class per repeat count
("double shot", "triple shot"); as decorators they are three classes and the
stack is assembled when the menu is wired.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Mapping
from typing import Protocol

from common import Money, ValidationError
from lld.vending_machine.models import Ingredient, Recipe


# --8<-- [start:beverages]
class Beverage(Protocol):
    """What the coffee bar needs from a drink: a name, a price, a bill of ingredients."""

    def name(self) -> str: ...

    def price(self) -> Money: ...

    def ingredients(self) -> Mapping[Ingredient, int]: ...


class BasicBeverage:
    """A drink straight from its recipe: espresso, latte, hot chocolate."""

    def __init__(self, recipe: Recipe) -> None:
        self._recipe = recipe

    def name(self) -> str:
        return self._recipe.name

    def price(self) -> Money:
        return self._recipe.price

    def ingredients(self) -> Mapping[Ingredient, int]:
        return dict(self._recipe.amounts)


class BeverageDecorator(ABC):
    """Same interface, one more responsibility. Decorators wrap decorators.

    Subclasses declare three immutable class attributes; the merging logic lives
    here once, so a new add-on is four lines and touches nothing else.
    """

    label: str
    surcharge: Money
    extra: tuple[tuple[Ingredient, int], ...]

    def __init__(self, drink: Beverage) -> None:
        self._drink = drink

    def name(self) -> str:
        return f"{self._drink.name()} + {self.label}"

    def price(self) -> Money:
        return self._drink.price() + self.surcharge

    def ingredients(self) -> Mapping[Ingredient, int]:
        merged = dict(self._drink.ingredients())
        for ingredient, amount in self.extra:
            merged[ingredient] = merged.get(ingredient, 0) + amount
        return merged


class ExtraShot(BeverageDecorator):
    label = "extra shot"
    surcharge = Money.of("0.50")
    extra = ((Ingredient.BEANS, 7), (Ingredient.WATER, 15))


class ExtraMilk(BeverageDecorator):
    label = "extra milk"
    surcharge = Money.of("0.25")
    extra = ((Ingredient.MILK, 50),)


class Sweetened(BeverageDecorator):
    label = "sugar"
    surcharge = Money(0)
    extra = ((Ingredient.SUGAR, 6),)


class BeverageFactory:
    """Factory Method with a registry: a recipe plus add-on names becomes a drink.

    The menu becomes configuration - `create(LATTE, "shot", "milk")` - so a new add-on is
    a decorator class plus one registry entry, and no call site changes.
    """

    _add_ons: dict[str, type[BeverageDecorator]] = {
        "shot": ExtraShot,
        "milk": ExtraMilk,
        "sugar": Sweetened,
    }

    @classmethod
    def create(cls, recipe: Recipe, *add_ons: str) -> Beverage:
        drink: Beverage = BasicBeverage(recipe)
        for name in add_ons:
            try:
                decorator = cls._add_ons[name]
            except KeyError:
                raise ValidationError(f"unknown add-on {name!r}") from None
            drink = decorator(drink)
        return drink


# --8<-- [end:beverages]
