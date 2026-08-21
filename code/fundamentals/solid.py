"""SOLID in Python: one smell, one refactor and one testable difference per principle.

Every principle appears twice on the same checkout domain - a ``*Before`` class that
violates it and the shape that fixes it - so the tests can assert what each refactor
actually bought.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, auto
from typing import Protocol, runtime_checkable

from common import ConflictError, Money, NotFoundError, ValidationError

STANDARD_FEE = Money.of("4.99")
EXPRESS_FEE = Money.of("9.99")
LOCKER_MINIMUM = Money.of("2.49")
LOCKER_PER_ITEM = Money.of("0.75")
DEFAULT_TAX_RATE = Decimal("0.20")


class ShippingMethod(StrEnum):
    STANDARD = auto()
    EXPRESS = auto()
    LOCKER = auto()


@dataclass(frozen=True, slots=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price: Money


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    customer_email: str
    lines: tuple[OrderLine, ...]
    shipping: ShippingMethod = ShippingMethod.STANDARD

    @property
    def subtotal(self) -> Money:
        total = Money(0)
        for line in self.lines:
            total += line.unit_price * line.quantity
        return total

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)


# --8<-- [start:srp_before]
class OrderServiceBefore:
    """One class, five reasons to change: what a valid order is, the shipping fees,
    the tax rate, where orders are stored and how the customer is told.
    """

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self.outbox: list[str] = []

    def place(self, order: Order) -> Money:
        if not order.lines:  # reason 1: validation rules
            raise ValidationError("an order needs at least one line")
        if "@" not in order.customer_email:
            raise ValidationError("invalid email address")
        total = order.subtotal
        if order.shipping is ShippingMethod.EXPRESS:  # reason 2: shipping fees
            total += EXPRESS_FEE
        else:
            total += STANDARD_FEE
        total += total * DEFAULT_TAX_RATE  # reason 3: the tax rate
        self._orders[order.order_id] = order  # reason 4: the storage backend
        self.outbox.append(f"Order {order.order_id}: {total}")  # reason 5: the template
        return total
# --8<-- [end:srp_before]


# --8<-- [start:srp_after]
class OrderValidator:
    """One reason to change: what counts as a valid order."""

    def check(self, order: Order) -> None:
        if not order.lines:
            raise ValidationError("an order needs at least one line")
        if "@" not in order.customer_email:
            raise ValidationError(f"{order.customer_email!r} is not an email address")


@dataclass(frozen=True, slots=True)
class TaxPolicy:
    """One reason to change: the tax rate."""

    rate: Decimal = DEFAULT_TAX_RATE

    def on(self, amount: Money) -> Money:
        return amount * self.rate


class InMemoryOrderRepository:
    """One reason to change: where orders live."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def save(self, order: Order) -> None:
        self._orders[order.order_id] = order

    def get(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise NotFoundError(f"no order {order_id!r}") from None
# --8<-- [end:srp_after]


# --8<-- [start:ocp_before]
class ShippingCalculatorBefore:
    """Adding a method edits this function, and every other one that switches on
    the same enum.
    """

    def cost(self, order: Order) -> Money:
        if order.shipping is ShippingMethod.STANDARD:
            return STANDARD_FEE
        if order.shipping is ShippingMethod.EXPRESS:
            return EXPRESS_FEE
        if order.shipping is ShippingMethod.LOCKER:
            return LOCKER_MINIMUM
        raise ValidationError(f"unknown shipping method {order.shipping}")
# --8<-- [end:ocp_before]


# --8<-- [start:ocp_after]
@runtime_checkable
class ShippingRule(Protocol):
    def cost(self, order: Order) -> Money: ...


@dataclass(frozen=True, slots=True)
class FlatShipping:
    fee: Money

    def cost(self, order: Order) -> Money:
        return self.fee


@dataclass(frozen=True, slots=True)
class WeightedShipping:
    """A rule the if/elif ladder could not express without growing a second switch."""

    per_item: Money
    minimum: Money

    def cost(self, order: Order) -> Money:
        return max(self.per_item * order.item_count, self.minimum)


@dataclass(frozen=True, slots=True)
class FreeOverThreshold:
    """Written months later, wrapping any rule, editing none of them."""

    inner: ShippingRule
    threshold: Money

    def cost(self, order: Order) -> Money:
        if order.subtotal >= self.threshold:
            return Money(0)
        return self.inner.cost(order)


class ShippingRegistry:
    """Open for extension through ``register``; ``cost_for`` never changes again."""

    def __init__(self, rules: Mapping[ShippingMethod, ShippingRule]) -> None:
        self._rules: dict[ShippingMethod, ShippingRule] = dict(rules)

    def register(self, method: ShippingMethod, rule: ShippingRule) -> None:
        self._rules[method] = rule

    def cost_for(self, order: Order) -> Money:
        try:
            rule = self._rules[order.shipping]
        except KeyError:
            raise ValidationError(f"no shipping rule for {order.shipping}") from None
        return rule.cost(order)


def default_shipping_registry() -> ShippingRegistry:
    return ShippingRegistry(
        {
            ShippingMethod.STANDARD: FlatShipping(STANDARD_FEE),
            ShippingMethod.EXPRESS: FlatShipping(EXPRESS_FEE),
            ShippingMethod.LOCKER: WeightedShipping(LOCKER_PER_ITEM, LOCKER_MINIMUM),
        }
    )
# --8<-- [end:ocp_after]


# --8<-- [start:lsp_before]
class RectangleBefore:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height

    def set_width(self, width: int) -> None:
        self.width = width

    def set_height(self, height: int) -> None:
        self.height = height

    def area(self) -> int:
        return self.width * self.height


class SquareBefore(RectangleBefore):
    """A square *is* a rectangle in geometry and *is not* one in code: it breaks
    the postcondition that setting the width leaves the height alone.
    """

    def __init__(self, side: int) -> None:
        super().__init__(side, side)

    def set_width(self, width: int) -> None:
        self.width = self.height = width

    def set_height(self, height: int) -> None:
        self.width = self.height = height
# --8<-- [end:lsp_before]


# --8<-- [start:lsp_after]
class Shape(Protocol):
    """What every shape shares is *area*, not independently mutable sides."""

    def area(self) -> int: ...


@dataclass(frozen=True, slots=True)
class Rectangle:
    width: int
    height: int

    def area(self) -> int:
        return self.width * self.height


@dataclass(frozen=True, slots=True)
class Square:
    side: int

    def area(self) -> int:
        return self.side * self.side


def total_area(shapes: Iterable[Shape]) -> int:
    """Works for every ``Shape`` ever written; that is what substitutable means."""
    return sum(shape.area() for shape in shapes)
# --8<-- [end:lsp_after]


# --8<-- [start:isp_before]
class PaymentGatewayBefore(ABC):
    """Five abstract methods, so every implementation must supply five, and the cash
    drawer grows ``refund``, ``subscribe``, ``tokenize`` and ``settlement_report``
    stubs that raise.
    """

    @abstractmethod
    def charge(self, amount: Money, token: str) -> str: ...

    @abstractmethod
    def refund(self, charge_id: str) -> None: ...

    @abstractmethod
    def subscribe(self, plan_id: str, token: str) -> str: ...

    @abstractmethod
    def tokenize(self, card_number: str) -> str: ...

    @abstractmethod
    def settlement_report(self, day: str) -> list[str]: ...
# --8<-- [end:isp_before]


# --8<-- [start:isp_after]
@runtime_checkable
class Charges(Protocol):
    """The whole interface the checkout needs: one method."""

    def charge(self, amount: Money, token: str) -> str: ...


@runtime_checkable
class Refunds(Protocol):
    def refund(self, charge_id: str) -> None: ...


class CashDrawer:
    """Implements exactly what it can do. No stubs that raise."""

    def __init__(self) -> None:
        self.taken: list[Money] = []

    def charge(self, amount: Money, token: str) -> str:
        self.taken.append(amount)
        return f"cash-{len(self.taken)}"


class CardGateway:
    """Implements both small protocols; a client that only charges still depends on
    ``Charges`` alone.
    """

    def __init__(self, approve: bool = True) -> None:
        self.approve = approve
        self.charges: dict[str, Money] = {}

    def charge(self, amount: Money, token: str) -> str:
        if not self.approve:
            raise ConflictError(f"card {token} declined")
        charge_id = f"ch-{len(self.charges) + 1}"
        self.charges[charge_id] = amount
        return charge_id

    def refund(self, charge_id: str) -> None:
        if charge_id not in self.charges:
            raise NotFoundError(f"no charge {charge_id!r}")
        del self.charges[charge_id]
# --8<-- [end:isp_after]


# --8<-- [start:dip]
class OrderRepository(Protocol):
    def save(self, order: Order) -> None: ...

    def get(self, order_id: str) -> Order: ...


class Notifier(Protocol):
    def send(self, to: str, message: str) -> None: ...


class CheckoutServiceBefore:
    """Constructs its own collaborators, so nothing can be substituted: a test charges
    a real gateway, and a second provider means editing this class.
    """

    def __init__(self) -> None:
        self._payments = CardGateway()
        self._orders = InMemoryOrderRepository()

    def place(self, order: Order, token: str) -> str:
        charge_id = self._payments.charge(order.subtotal, token)
        self._orders.save(order)
        return charge_id


@dataclass(frozen=True, slots=True)
class Receipt:
    order_id: str
    subtotal: Money
    shipping: Money
    tax: Money
    total: Money
    charge_id: str


class CheckoutService:
    """Depends on five abstractions and constructs none of them: swapping the card
    gateway for a cash drawer is a change in ``main``, not in here.
    """

    def __init__(
        self,
        validator: OrderValidator,
        shipping: ShippingRegistry,
        tax: TaxPolicy,
        payments: Charges,
        orders: OrderRepository,
        notifier: Notifier,
    ) -> None:
        self._validator = validator
        self._shipping = shipping
        self._tax = tax
        self._payments = payments
        self._orders = orders
        self._notifier = notifier

    def place(self, order: Order, token: str) -> Receipt:
        self._validator.check(order)
        shipping = self._shipping.cost_for(order)
        taxable = order.subtotal + shipping
        tax = self._tax.on(taxable)
        total = taxable + tax
        charge_id = self._payments.charge(total, token)
        self._orders.save(order)
        self._notifier.send(order.customer_email, f"Order {order.order_id} total {total}")
        return Receipt(order.order_id, order.subtotal, shipping, tax, total, charge_id)
# --8<-- [end:dip]


class RecordingNotifier:
    """A fake worth two mocks: it records, so tests assert on real data."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, message: str) -> None:
        self.sent.append((to, message))


def build_checkout(payments: Charges, notifier: Notifier) -> CheckoutService:
    """The composition root: the one place that names concrete classes."""
    return CheckoutService(
        validator=OrderValidator(),
        shipping=default_shipping_registry(),
        tax=TaxPolicy(),
        payments=payments,
        orders=InMemoryOrderRepository(),
        notifier=notifier,
    )


def main() -> None:
    order = Order(
        "ORD-1",
        "ana@example.com",
        (OrderLine("SKU-1", 2, Money.of("19.99")), OrderLine("SKU-2", 1, Money.of("5.00"))),
        ShippingMethod.EXPRESS,
    )
    print(f"--- order {order.order_id}: subtotal {order.subtotal}, {order.item_count} items ---")

    legacy = OrderServiceBefore()
    print(f"SRP before: one class returns {legacy.place(order)} and owns the outbox too")
    notifier = RecordingNotifier()
    checkout = build_checkout(CardGateway(), notifier)
    receipt = checkout.place(order, token="tok-visa")
    print(f"SRP after:  {receipt.subtotal} + {receipt.shipping} + {receipt.tax} = {receipt.total}")

    ladder, registry = ShippingCalculatorBefore(), default_shipping_registry()
    bulk = (OrderLine("SKU-3", 6, Money.of("3.00")),)
    locker = Order("ORD-2", "bo@example.com", bulk, ShippingMethod.LOCKER)
    print(f"OCP before: locker is {ladder.cost(locker)} whatever the {locker.item_count}-item basket holds")
    print(f"OCP after:  the same basket is {registry.cost_for(locker)} under a per-item rule")
    registry.register(ShippingMethod.EXPRESS, FreeOverThreshold(FlatShipping(EXPRESS_FEE), Money.of("40.00")))
    print(f"OCP after:  a wrapping rule registered later drops express to {registry.cost_for(order)}")

    square = SquareBefore(4)
    square.set_width(5)
    print(f"LSP before: set_width(5) on a 4x4 square gives area {square.area()}, not 20")
    print(f"LSP after:  total_area of a rectangle and a square = {total_area([Rectangle(5, 4), Square(4)])}")

    drawer = CashDrawer()
    print(f"ISP after:  the cash drawer charges ({drawer.charge(receipt.total, 'cash')}) and owes no refund stub")
    print(f"ISP after:  isinstance(drawer, Charges) -> {isinstance(drawer, Charges)}, Refunds -> {isinstance(drawer, Refunds)}")

    cash_checkout = build_checkout(drawer, notifier)
    print(f"DIP:        the same service on a cash drawer -> {cash_checkout.place(order, 'cash').charge_id}")
    try:
        build_checkout(CardGateway(approve=False), notifier).place(order, token="tok-declined")
    except ConflictError as exc:
        print(f"DIP:        the declined path is a two-line fake, not a sandbox: {exc}")
    print(f"notifier recorded {len(notifier.sent)} messages, the last to {notifier.sent[-1][0]}")


if __name__ == "__main__":
    main()
