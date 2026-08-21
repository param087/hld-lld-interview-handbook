"""Design principles beyond SOLID, worked on one small checkout domain.

DRY, KISS, YAGNI, the Law of Demeter, tell-don't-ask, the nine GRASP
responsibility questions, coupling and cohesion, fail fast and composition over
inheritance are all answers to the same question: *which object should own this
piece of work?* Every responsibility below is assigned exactly once, and the
docstring on each class names the principle that put it there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError

BASIS_POINTS = Decimal(10_000)
POINT_VALUE_CENTS = 1


# --8<-- [start:dry]
def percentage_of(amount: Money, basis_points: int) -> Money:
    """The one rounding rule every percentage in the system goes through.

    Tax and discounts share this genuinely: change how a fraction of a cent
    rounds and both must change together, or the invoice stops adding up. That
    shared *reason to change* is what makes it real duplication worth removing.
    """
    if basis_points < 0:
        raise ValidationError("basis points cannot be negative")
    return amount * (Decimal(basis_points) / BASIS_POINTS)


@dataclass(frozen=True, slots=True)
class TaxRate:
    """Set by tax law. Same shape as ``DiscountRate``, different reason to change."""

    basis_points: int

    def on(self, subtotal: Money) -> Money:
        return percentage_of(subtotal, self.basis_points)


@dataclass(frozen=True, slots=True)
class DiscountRate:
    """Set by marketing. Merging it with ``TaxRate`` because the code matches is
    accidental duplication: the next campaign would drag tax law along with it."""

    basis_points: int
    campaign: str

    def on(self, subtotal: Money) -> Money:
        return percentage_of(subtotal, self.basis_points)


# --8<-- [end:dry]


class OrderStatus(StrEnum):
    DRAFT = "draft"
    PLACED = "placed"


# --8<-- [start:entities]
@dataclass(frozen=True, slots=True)
class Address:
    """Fail fast: an ``Address`` that exists is an ``Address`` that is valid, so no
    downstream caller ever has to re-check the postcode."""

    line1: str
    city: str
    postcode: str
    country: str = "US"

    def __post_init__(self) -> None:
        if not self.postcode.strip():
            raise ValidationError("an address needs a postcode")


@dataclass(frozen=True, slots=True)
class Customer:
    id: str
    name: str
    address: Address


@dataclass(frozen=True, slots=True)
class OrderLine:
    sku: str
    unit_price: Money
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValidationError(f"line {self.sku!r} needs a positive quantity")

    @property
    def subtotal(self) -> Money:
        return self.unit_price * self.quantity


@dataclass(slots=True)
class Order:
    """Information Expert: the order holds the lines, so the order does the arithmetic.

    Creator, for the same reason: it builds its own ``OrderLine`` values, so no
    caller can hand it a half-built one. ``ship_to_postcode`` exists to keep the
    Law of Demeter: callers of an order talk to the order, not to the customer's
    address's postcode.
    """

    id: str
    customer: Customer
    status: OrderStatus = OrderStatus.DRAFT
    _lines: list[OrderLine] = field(default_factory=list)

    def add_line(self, sku: str, unit_price: Money, quantity: int) -> OrderLine:
        if self.status is not OrderStatus.DRAFT:
            raise InvalidStateError(f"order {self.id} is {self.status}, not draft")
        line = OrderLine(sku, unit_price, quantity)
        self._lines.append(line)
        return line

    @property
    def lines(self) -> tuple[OrderLine, ...]:
        return tuple(self._lines)

    def subtotal(self) -> Money:
        total = Money(0)
        for line in self._lines:
            total += line.subtotal
        return total

    def ship_to_postcode(self) -> str:
        return self.customer.address.postcode

    def place(self) -> None:
        """Tell-don't-ask: the order guards its own transition instead of exposing
        ``status`` for a service to set, which is where double-submits come from."""
        if self.status is not OrderStatus.DRAFT:
            raise InvalidStateError(f"order {self.id} is {self.status}, not draft")
        if not self._lines:
            raise ValidationError(f"order {self.id} has nothing in it")
        self.status = OrderStatus.PLACED


# --8<-- [end:entities]


# --8<-- [start:tell]
@dataclass(slots=True)
class LoyaltyAccount:
    """Tell-don't-ask: one call decides *and* applies, so no caller can skip the check.

    ``points`` stays readable because reporting needs it; what moved inside is the
    *decision*, which belongs with the data it depends on.
    """

    customer_id: str
    points: int = 0

    def earn(self, points: int) -> None:
        if points <= 0:
            raise ValidationError("earned points must be positive")
        self.points += points

    def redeem(self, points: int) -> Money:
        if points <= 0:
            raise ValidationError("redeemed points must be positive")
        if points > self.points:
            raise ConflictError(f"account {self.customer_id} has {self.points} points, not {points}")
        self.points -= points
        return Money(points * POINT_VALUE_CENTS)


# --8<-- [end:tell]


# --8<-- [start:composition]
type ShippingRule = Callable[[Money], Money]


def flat_shipping(cost: Money) -> ShippingRule:
    return lambda _subtotal: cost


def free_over(threshold: Money, inner: ShippingRule) -> ShippingRule:
    """Composition over inheritance: *free over N* wraps any rule, present or future.

    A ``FreeOverFlatShipping`` subclass would have to be written again for weight-based
    shipping, again for courier shipping, and again for whatever arrives next quarter.
    """

    def rule(subtotal: Money) -> Money:
        return Money(0, threshold.currency) if subtotal >= threshold else inner(subtotal)

    return rule


# --8<-- [end:composition]


# --8<-- [start:grasp]
class TaxPolicy(Protocol):
    """Protected Variations: tax law is the likeliest thing to change, so it gets a seam."""

    def tax_for(self, subtotal: Money, postcode: str) -> Money: ...


@dataclass(frozen=True, slots=True)
class DestinationTax:
    """Rates keyed by postcode prefix, longest prefix first. A new state is a new tuple entry."""

    rates_by_prefix: tuple[tuple[str, int], ...] = ()
    default_basis_points: int = 0

    def tax_for(self, subtotal: Money, postcode: str) -> Money:
        for prefix, basis_points in self.rates_by_prefix:
            if postcode.startswith(prefix):
                return TaxRate(basis_points).on(subtotal)
        return TaxRate(self.default_basis_points).on(subtotal)


class OrderRepository(Protocol):
    """Pure Fabrication plus Indirection: no repository exists in the business, but
    inventing one keeps storage out of ``Order`` and keeps the service off a database."""

    def save(self, order: Order) -> None: ...

    def get(self, order_id: str) -> Order: ...


class InMemoryOrderRepository:
    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def save(self, order: Order) -> None:
        self._orders[order.id] = order

    def get(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise NotFoundError(f"no order {order_id!r}") from None


@dataclass(frozen=True, slots=True)
class Invoice:
    order_id: str
    subtotal: Money
    tax: Money
    shipping: Money

    @property
    def total(self) -> Money:
        return self.subtotal + self.tax + self.shipping


class CheckoutService:
    """Controller: one class per use case, sequencing steps and owning no rules of its own.

    Low Coupling by construction — it names two Protocols and a callable, never a
    concrete class — and High Cohesion because every line of it is about checking
    one order out. Move the tax arithmetic in here and both properties are gone.
    """

    def __init__(self, orders: OrderRepository, tax: TaxPolicy, shipping: ShippingRule) -> None:
        self._orders = orders
        self._tax = tax
        self._shipping = shipping

    def checkout(self, order_id: str) -> Invoice:
        order = self._orders.get(order_id)
        subtotal = order.subtotal()
        invoice = Invoice(
            order_id=order.id,
            subtotal=subtotal,
            tax=self._tax.tax_for(subtotal, order.ship_to_postcode()),
            shipping=self._shipping(subtotal),
        )
        order.place()
        self._orders.save(order)
        return invoice


# --8<-- [end:grasp]


def main() -> None:
    customer = Customer("C-1", "Ada", Address("1 Market St", "San Francisco", "94107"))
    order = Order("O-1", customer)
    order.add_line("SKU-1", Money.of("29.99"), 2)
    order.add_line("SKU-2", Money.of("9.99"), 1)
    order.add_line("SKU-3", Money.of("19.99"), 1)

    print("--- the object that owns the data does the arithmetic ---")
    print(f"{len(order.lines)} lines, subtotal {order.subtotal()}, ships to {order.ship_to_postcode()}")

    print("--- fail fast: an invalid line never becomes an object ---")
    try:
        order.add_line("SKU-9", Money.of("1.00"), 0)
    except ValidationError as exc:
        print(f"rejected: {exc}")

    repository = InMemoryOrderRepository()
    repository.save(order)
    threshold = Money.of("50.00")
    service = CheckoutService(
        orders=repository,
        tax=DestinationTax(rates_by_prefix=(("941", 875), ("9", 600)), default_basis_points=0),
        shipping=free_over(threshold, flat_shipping(Money.of("7.50"))),
    )
    invoice = service.checkout("O-1")
    print("--- the controller sequences, the policies decide ---")
    print(f"tax:      {invoice.tax}")
    print(f"shipping: {invoice.shipping} (free over {threshold})")
    print(f"total:    {invoice.total}")

    print("--- tell-don't-ask: the account applies its own rule ---")
    account = LoyaltyAccount("C-1")
    account.earn(500)
    print(f"earned 500, redeemed 200 -> {account.redeem(200)} credit, {account.points} points left")
    try:
        account.redeem(1000)
    except ConflictError as exc:
        print(f"rejected: {exc}")

    print("--- a second checkout is refused by the order, not by the service ---")
    try:
        service.checkout("O-1")
    except InvalidStateError as exc:
        print(f"rejected: {exc}")
    try:
        service.checkout("O-404")
    except NotFoundError as exc:
        print(f"rejected: {exc}")


if __name__ == "__main__":
    main()
