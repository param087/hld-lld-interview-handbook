"""The menu as a Composite: sections contain items, combos contain items, both price.

A section and a combo differ in exactly one place, and it is the interesting one:
a section is orderable if *any* child is available, a combo only if *every* child is.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from common import Money
from lld.restaurant_management.models import Modifier, UnknownItemError


# --8<-- [start:composite]
class MenuComponent(ABC):
    """Uniform interface: everything on the menu has an id, a price and availability."""

    def __init__(self, component_id: str, name: str) -> None:
        self.id = component_id
        self.name = name

    @abstractmethod
    def price(self) -> Money: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def leaves(self) -> list[MenuItem]:
        """Every orderable dish underneath this node (itself, if it is a leaf)."""

    def find(self, component_id: str) -> MenuComponent | None:
        return self if self.id == component_id else None

    def describe(self, indent: int = 0) -> str:
        mark = "" if self.is_available() else " (86'd)"
        return f"{' ' * indent}{self.name} {self.price()}{mark}"


class MenuItem(MenuComponent):
    """A leaf: one dish, one price, its own allowed modifiers."""

    def __init__(
        self,
        component_id: str,
        name: str,
        unit_price: Money,
        available: bool = True,
        modifiers: tuple[Modifier, ...] = (),
    ) -> None:
        super().__init__(component_id, name)
        self.unit_price = unit_price
        self.available = available
        self.modifiers = modifiers

    def price(self) -> Money:
        return self.unit_price

    def is_available(self) -> bool:
        return self.available

    def leaves(self) -> list[MenuItem]:
        return [self]


class MenuSection(MenuComponent):
    """A composite: Starters, Mains, Desserts. Available if anything in it is."""

    def __init__(self, component_id: str, name: str, children: list[MenuComponent] | None = None) -> None:
        super().__init__(component_id, name)
        self._children: list[MenuComponent] = list(children or [])

    def add(self, child: MenuComponent) -> MenuSection:
        self._children.append(child)
        return self

    def children(self) -> list[MenuComponent]:
        return list(self._children)

    def price(self) -> Money:
        total = Money(0)
        for child in self._children:
            total = total + child.price()
        return total

    def is_available(self) -> bool:
        return any(child.is_available() for child in self._children)

    def leaves(self) -> list[MenuItem]:
        return [leaf for child in self._children for leaf in child.leaves()]

    def find(self, component_id: str) -> MenuComponent | None:
        if self.id == component_id:
            return self
        for child in self._children:
            found = child.find(component_id)
            if found is not None:
                return found
        return None

    def require(self, component_id: str) -> MenuComponent:
        found = self.find(component_id)
        if found is None:
            raise UnknownItemError(f"no menu component {component_id!r}")
        return found

    def describe(self, indent: int = 0) -> str:
        lines = [f"{' ' * indent}{self.name}"]
        lines.extend(child.describe(indent + 2) for child in self._children)
        return "\n".join(lines)


class ComboItem(MenuSection):
    """A composite you can order: sum of its parts, minus a discount, all-or-nothing."""

    def __init__(
        self,
        component_id: str,
        name: str,
        children: list[MenuComponent],
        discount: Decimal = Decimal("0.15"),
    ) -> None:
        super().__init__(component_id, name, children)
        self._keep = Decimal(1) - discount

    def price(self) -> Money:
        return super().price() * self._keep

    def is_available(self) -> bool:
        return all(child.is_available() for child in self.children())


# --8<-- [end:composite]
