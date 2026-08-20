"""Specification: composable business rules combined with ``&``, ``|`` and ``~``.

The running example is catalogue filtering. ``Product`` is the candidate,
``InStock``, ``PriceBelow`` and ``InCategory`` are leaf rules, and the three
composite nodes combine them into a tree that ``Catalog.search`` evaluates.
Every node answers one yes/no question through ``is_satisfied_by``, so a leaf
and a tree of forty leaves are interchangeable, and because the tree is data
it can also be printed, compared or translated instead of evaluated.
The second half restates the rules as plain predicates, the Pythonic form
when nobody needs to name, inspect or serialise a rule.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import reduce
from operator import and_, or_

from common import Money, ValidationError


@dataclass(frozen=True, slots=True)
class Product:
    """The candidate every rule is asked about."""

    sku: str
    name: str
    price: Money
    stock: int
    category: str


# --8<-- [start:specification]
class Specification[T](ABC):
    """The Specification interface plus the algebra every rule inherits.

    An ``ABC`` rather than a ``Protocol`` because ``&``, ``|`` and ``~`` are
    shared behaviour: every rule gets them for free and no rule can forget one.
    ``and``, ``or`` and ``not`` cannot be overloaded in Python, hence the
    bitwise operators (the same choice Django's ``Q`` and SQLAlchemy made).
    """

    @abstractmethod
    def is_satisfied_by(self, candidate: T) -> bool: ...

    @abstractmethod
    def describe(self) -> str:
        """A readable rendering of the rule; the tree is data, not only behaviour."""

    def __call__(self, candidate: T) -> bool:
        return self.is_satisfied_by(candidate)

    def __and__(self, other: Specification[T]) -> Specification[T]:
        return AndSpecification(self, other)

    def __or__(self, other: Specification[T]) -> Specification[T]:
        return OrSpecification(self, other)

    def __invert__(self) -> Specification[T]:
        return NotSpecification(self)


@dataclass(frozen=True, slots=True)
class AndSpecification[T](Specification[T]):
    left: Specification[T]
    right: Specification[T]

    def is_satisfied_by(self, candidate: T) -> bool:
        return self.left.is_satisfied_by(candidate) and self.right.is_satisfied_by(candidate)

    def describe(self) -> str:
        return f"({self.left.describe()} AND {self.right.describe()})"


@dataclass(frozen=True, slots=True)
class OrSpecification[T](Specification[T]):
    left: Specification[T]
    right: Specification[T]

    def is_satisfied_by(self, candidate: T) -> bool:
        return self.left.is_satisfied_by(candidate) or self.right.is_satisfied_by(candidate)

    def describe(self) -> str:
        return f"({self.left.describe()} OR {self.right.describe()})"


@dataclass(frozen=True, slots=True)
class NotSpecification[T](Specification[T]):
    inner: Specification[T]

    def is_satisfied_by(self, candidate: T) -> bool:
        return not self.inner.is_satisfied_by(candidate)

    def describe(self) -> str:
        return f"NOT {self.inner.describe()}"


# --8<-- [end:specification]


# --8<-- [start:leaves]
@dataclass(frozen=True, slots=True)
class InStock(Specification[Product]):
    """A leaf with no configuration: the rule is the class."""

    def is_satisfied_by(self, candidate: Product) -> bool:
        return candidate.stock > 0

    def describe(self) -> str:
        return "in_stock"


@dataclass(frozen=True, slots=True)
class PriceBelow(Specification[Product]):
    """A leaf with configuration, fixed at construction and immutable afterwards."""

    limit: Money

    def is_satisfied_by(self, candidate: Product) -> bool:
        return candidate.price < self.limit

    def describe(self) -> str:
        return f"price < {self.limit}"


@dataclass(frozen=True, slots=True)
class InCategory(Specification[Product]):
    category: str

    def is_satisfied_by(self, candidate: Product) -> bool:
        return candidate.category == self.category

    def describe(self) -> str:
        return f"category = {self.category}"


class Catalog:
    """The client: owns the products, evaluates any rule tree, never inspects it."""

    def __init__(self, products: Iterable[Product]) -> None:
        self._products = tuple(products)

    def __len__(self) -> int:
        return len(self._products)

    @property
    def products(self) -> tuple[Product, ...]:
        return self._products

    def search(self, spec: Specification[Product]) -> list[Product]:
        return [product for product in self._products if spec.is_satisfied_by(product)]


def all_of[T](*specs: Specification[T]) -> Specification[T]:
    """Fold the rules a filter panel collected: ``reduce`` over ``&`` builds the tree."""
    if not specs:
        raise ValidationError("all_of needs at least one specification")
    return reduce(and_, specs)


def any_of[T](*specs: Specification[T]) -> Specification[T]:
    if not specs:
        raise ValidationError("any_of needs at least one specification")
    return reduce(or_, specs)


# --8<-- [end:leaves]


# --8<-- [start:functional]
# A rule with one method is a function: ``Predicate`` is the whole interface.
type Predicate[T] = Callable[[T], bool]


def every[T](*preds: Predicate[T]) -> Predicate[T]:
    return lambda candidate: all(pred(candidate) for pred in preds)


def some[T](*preds: Predicate[T]) -> Predicate[T]:
    return lambda candidate: any(pred(candidate) for pred in preds)


def negate[T](pred: Predicate[T]) -> Predicate[T]:
    return lambda candidate: not pred(candidate)


def in_stock(product: Product) -> bool:
    return product.stock > 0


def price_below(limit: Money) -> Predicate[Product]:
    """A closure carries the configuration that the dataclass field carried above."""
    return lambda product: product.price < limit


def in_category(category: str) -> Predicate[Product]:
    return lambda product: product.category == category


# --8<-- [end:functional]


def sample_catalog() -> Catalog:
    return Catalog(
        [
            Product("B-1", "Python Cookbook", Money.of("45.00"), 12, "books"),
            Product("B-2", "Collectors Atlas", Money.of("180.00"), 3, "books"),
            Product("T-1", "Wooden Train", Money.of("30.00"), 0, "toys"),
            Product("T-2", "Chess Set", Money.of("60.00"), 7, "toys"),
            Product("E-1", "Headphones", Money.of("99.00"), 0, "electronics"),
            Product("E-2", "USB-C Hub", Money.of("25.00"), 40, "electronics"),
        ]
    )


def main() -> None:
    catalog = sample_catalog()
    under_100 = PriceBelow(Money.of("100.00"))
    bargain = InStock() & under_100
    print(f"--- {bargain.describe()} ---")
    for product in catalog.search(bargain):
        print(f"  {product.sku:<4} {product.name:<17} {product.price}")

    gift = (bargain & ~InCategory("electronics")) | InCategory("books")
    print(f"--- {gift.describe()} ---")
    for product in catalog.search(gift):
        print(f"  {product.sku:<4} {product.name:<17} {product.price}")

    print("--- a filter panel folds its selections with all_of(...) ---")
    panel = all_of(InStock(), under_100, InCategory("electronics"))
    print(f"  {panel.describe()}")
    print(f"  matches: {[product.sku for product in catalog.search(panel)]}")
    rebuilt = InStock() & under_100 & InCategory("electronics")
    print(f"  rules are values: rebuilt tree == panel -> {rebuilt == panel}")

    print("--- functional variant: plain predicates, same answers ---")
    bargain_fn = every(in_stock, price_below(Money.of("100.00")))
    gift_fn = some(every(bargain_fn, negate(in_category("electronics"))), in_category("books"))
    for label, spec, pred in (("bargain", bargain, bargain_fn), ("gift", gift, gift_fn)):
        same = catalog.search(spec) == list(filter(pred, catalog.products))
        print(f"  {label:<8} classes and predicates agree: {same}")


if __name__ == "__main__":
    main()
