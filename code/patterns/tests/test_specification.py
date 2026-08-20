"""Specification: leaves answer one question, the algebra composes them, and predicates qualify too."""

import pytest

from common import Money, ValidationError
from patterns.specification import (
    AndSpecification,
    Catalog,
    InCategory,
    InStock,
    NotSpecification,
    OrSpecification,
    PriceBelow,
    Product,
    Specification,
    all_of,
    any_of,
    every,
    in_category,
    in_stock,
    negate,
    price_below,
    sample_catalog,
    some,
)

CHEAP_BOOK = Product("B-1", "Python Cookbook", Money.of("45.00"), 12, "books")
DEAR_BOOK = Product("B-2", "Collectors Atlas", Money.of("180.00"), 3, "books")
SOLD_OUT_TOY = Product("T-1", "Wooden Train", Money.of("30.00"), 0, "toys")
UNDER_100 = PriceBelow(Money.of("100.00"))


@pytest.mark.parametrize(
    ("spec", "product", "expected"),
    [
        (InStock(), CHEAP_BOOK, True),
        (InStock(), SOLD_OUT_TOY, False),
        (UNDER_100, CHEAP_BOOK, True),
        (UNDER_100, DEAR_BOOK, False),
        (PriceBelow(Money.of("45.00")), CHEAP_BOOK, False),  # strictly below
        (InCategory("books"), DEAR_BOOK, True),
        (InCategory("books"), SOLD_OUT_TOY, False),
    ],
)
def test_each_leaf_answers_exactly_one_question(
    spec: Specification[Product], product: Product, expected: bool
) -> None:
    assert spec.is_satisfied_by(product) is expected
    assert spec(product) is expected  # __call__ makes a rule usable with filter()


@pytest.mark.parametrize("product", [CHEAP_BOOK, DEAR_BOOK, SOLD_OUT_TOY])
def test_operators_follow_boolean_algebra(product: Product) -> None:
    a, b = InStock(), UNDER_100
    assert (a & b)(product) is (a(product) and b(product))
    assert (a | b)(product) is (a(product) or b(product))
    assert (~a)(product) is (not a(product))
    assert (~(a & b))(product) is ((~a) | (~b))(product)  # De Morgan holds for the tree


def test_operators_build_the_expected_tree_and_describe_renders_it() -> None:
    tree = (InStock() & UNDER_100) | ~InCategory("toys")
    assert isinstance(tree, OrSpecification)
    assert isinstance(tree.left, AndSpecification)
    assert isinstance(tree.right, NotSpecification)
    assert tree.describe() == "((in_stock AND price < 100.00 USD) OR NOT category = toys)"


def test_rules_are_values_equal_by_structure_and_hashable() -> None:
    panel = all_of(InStock(), UNDER_100, InCategory("books"))
    assert panel == InStock() & UNDER_100 & InCategory("books")
    assert panel != any_of(InStock(), UNDER_100, InCategory("books"))
    assert len({panel, InStock() & UNDER_100 & InCategory("books")}) == 1
    with pytest.raises(ValidationError):
        all_of()


def test_catalog_evaluates_any_tree_and_preserves_order() -> None:
    catalog = sample_catalog()
    bargains = catalog.search(InStock() & UNDER_100)
    assert [p.sku for p in bargains] == ["B-1", "T-2", "E-2"]
    assert catalog.search(InStock() & ~InStock()) == []
    assert len(catalog.search(any_of(InStock(), ~InStock()))) == len(catalog) == 6


def test_a_new_leaf_needs_no_change_to_existing_rules() -> None:
    class NameContains(Specification[Product]):
        def __init__(self, text: str) -> None:
            self.text = text

        def is_satisfied_by(self, candidate: Product) -> bool:
            return self.text.lower() in candidate.name.lower()

        def describe(self) -> str:
            return f"name ~ {self.text}"

    spec = NameContains("python") & InStock()
    assert [p.sku for p in sample_catalog().search(spec)] == ["B-1"]
    assert spec.describe() == "(name ~ python AND in_stock)"


@pytest.mark.parametrize("product", sample_catalog().products)
def test_predicates_agree_with_the_classes(product: Product) -> None:
    classes = (InStock() & UNDER_100 & ~InCategory("electronics")) | InCategory("books")
    predicates = some(
        every(in_stock, price_below(Money.of("100.00")), negate(in_category("electronics"))),
        in_category("books"),
    )
    assert predicates(product) is classes(product)


def test_catalog_is_an_immutable_snapshot() -> None:
    products = [CHEAP_BOOK]
    catalog = Catalog(products)
    products.append(DEAR_BOOK)
    assert len(catalog) == 1 and catalog.products == (CHEAP_BOOK,)
