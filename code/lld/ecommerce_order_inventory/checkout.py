"""The checkout facade: idempotency key, reserve, price, pay, commit, publish."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock
from lld.ecommerce_order_inventory.events import (
    ORDER_CANCELLED,
    ORDER_PAID,
    ORDER_PLACED,
    ORDER_SHIPPED,
    EventBus,
)
from lld.ecommerce_order_inventory.inventory import InventoryService
from lld.ecommerce_order_inventory.models import (
    RESTOCK_ON_CANCEL_FROM,
    Address,
    CheckoutInProgressError,
    Event,
    HoldExpiredError,
    Order,
    OrderStateError,
    OrderStatus,
    Payment,
    PaymentDeclinedError,
    PaymentStatus,
    PriceChangedError,
    ShippingSpeed,
)
from lld.ecommerce_order_inventory.repository import InMemoryRepository, UnitOfWork
from lld.ecommerce_order_inventory.services import (
    CartService,
    CatalogService,
    OrderService,
    snapshot_items,
)
from lld.ecommerce_order_inventory.strategies import (
    ApprovingGateway,
    DiscountStrategy,
    NoDiscount,
    PaymentGateway,
    RegionTax,
    ShippingStrategy,
    TaxCalculator,
    WeightBandShipping,
)


@dataclass(slots=True)
class CheckoutRecord:
    """One row of the idempotency store: in progress until ``order_id`` is set."""

    key: str
    started_at: float
    order_id: str | None = None


# --8<-- [start:checkout]
class CheckoutFacade:
    """Turn a cart into a paid order, or leave nothing behind.

    Checkout is deliberately **two calls**, because a real customer sits on the
    payment page for minutes:

    * ``checkout`` claims an idempotency key, prices the basket, reserves stock
      all-or-nothing, and writes an order in ``CREATED``. The units are now
      *held*, with a TTL running.
    * ``pay`` authorises the card, commits the held units and moves the order to
      ``PAID`` -- or, if the customer took too long and the sweeper already gave
      the units back, fails cleanly and cancels the order.

    Two rules run through both. **Compensate, do not roll back, across services:**
    stock lives in ``InventoryService``, so undoing a hold is a release call, not
    a database rollback. **Publish after committing:** events go out once the
    unit of work has closed, because publishing inside it is how stores send
    "order received" emails for orders that never existed.
    """

    def __init__(
        self,
        catalog: CatalogService,
        carts: CartService,
        inventory: InventoryService,
        orders: OrderService,
        bus: EventBus,
        discount: DiscountStrategy | None = None,
        tax: TaxCalculator | None = None,
        shipping: ShippingStrategy | None = None,
        gateway: PaymentGateway | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self.catalog = catalog
        self.carts = carts
        self.inventory = inventory
        self.orders = orders
        self.bus = bus
        self.payments = InMemoryRepository("payment")
        self._discount = discount or NoDiscount()
        self._tax = tax or RegionTax({})
        self._shipping = shipping or WeightBandShipping()
        self._gateway = gateway or ApprovingGateway()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("PAY")
        self._uow = UnitOfWork(orders=orders.repository, payments=self.payments)
        self._checkouts: dict[str, CheckoutRecord] = {}
        self._lock = threading.Lock()

    def checkout(
        self,
        cart_id: str,
        customer_id: str,
        ship_to: Address,
        idempotency_key: str,
        speed: ShippingSpeed = ShippingSpeed.STANDARD,
        expected_total: Money | None = None,
    ) -> Order:
        """Price, hold stock, and create the order. Safe to call twice with one key."""
        replay = self._claim_key(idempotency_key)
        if replay is not None:
            return replay  # the same request arrived twice; nothing is bought twice

        hold = None
        try:
            items = snapshot_items(self.catalog, self.carts.cart(cart_id))
            discount = self._discount.discount(items)
            subtotal = Money(sum(item.line_total.cents for item in items))
            tax = self._tax.tax(subtotal - discount, ship_to)
            shipping = self._shipping.cost(items, ship_to, speed)
            total = subtotal - discount + tax + shipping
            if expected_total is not None and expected_total != total:
                raise PriceChangedError(f"basket is now {total}, you were shown {expected_total}")

            hold = self.inventory.reserve({i.sku_id: i.quantity for i in items}, owner=idempotency_key)
            order = Order(
                id=self.orders.next_id(),
                customer_id=customer_id,
                items=items,
                ship_to=ship_to,
                discount=discount,
                tax=tax,
                shipping=shipping,
                hold_id=hold.id,
                idempotency_key=idempotency_key,
                placed_at=self._clock.now(),
            )
            self.orders.repository.add(order)
        except Exception:
            if hold is not None:
                self._safe_release(hold.id)  # compensating call, not a rollback
            self._abandon_key(idempotency_key)
            raise

        self._complete_key(idempotency_key, order.id)
        self._publish(ORDER_PLACED, order)
        return order

    def pay(self, order_id: str) -> Payment:
        """Authorise, commit the held units, and move to PAID -- all or none of it.

        The unit of work earns its place here and not in ``checkout``: two
        repositories change together, and a decline must leave neither behind.
        """
        order = self.orders.order(order_id)
        if order.status is not OrderStatus.CREATED:
            raise OrderStateError(f"order {order_id} is {order.status}, not awaiting payment")
        payment = Payment(self._ids.next_id(), order.id, order.total, PaymentStatus.CAPTURED)
        try:
            with self._uow:
                if not self._gateway.authorize(order.total, order.id):
                    raise PaymentDeclinedError(f"card declined for {order.total}")
                self._uow.payments.add(payment)
                self.inventory.commit(order.hold_id)  # raises if the TTL sweeper won the race
                self.orders.transition(order.id, OrderStatus.PAID)
        except (PaymentDeclinedError, HoldExpiredError):
            self._safe_release(order.hold_id)
            self.orders.transition(order.id, OrderStatus.CANCELLED)
            self._publish(ORDER_CANCELLED, order)
            raise
        self._publish(ORDER_PAID, order)
        return payment

    # -- fulfilment --------------------------------------------------------------
    def pack(self, order_id: str) -> Order:
        return self.orders.transition(order_id, OrderStatus.PACKED)

    def ship(self, order_id: str) -> Order:
        order = self.orders.transition(order_id, OrderStatus.SHIPPED)
        self._publish(ORDER_SHIPPED, order)
        return order

    def deliver(self, order_id: str) -> Order:
        return self.orders.transition(order_id, OrderStatus.DELIVERED)

    def cancel(self, order_id: str) -> Order:
        """Give the units back the right way: release a hold, restock a commit."""
        previous = self.orders.order(order_id).status
        order = self.orders.transition(order_id, OrderStatus.CANCELLED)
        if previous is OrderStatus.CREATED:
            self._safe_release(order.hold_id)
        elif previous in RESTOCK_ON_CANCEL_FROM:
            self._restock(order)
            self._refund(order)
        self._publish(ORDER_CANCELLED, order)
        return order

    def accept_return(self, order_id: str) -> Order:
        order = self.orders.transition(order_id, OrderStatus.RETURNED)
        self._restock(order)
        self._refund(order)
        return order

    def sweep_expired_holds(self) -> list[str]:
        """Abandoned checkouts give their units back and their orders are cancelled."""
        cancelled = []
        for expired in self.inventory.expire_holds():
            for order in self.orders.repository.all():
                if order.hold_id == expired.id and order.status is OrderStatus.CREATED:
                    self.orders.transition(order.id, OrderStatus.CANCELLED)
                    cancelled.append(order.id)
        return cancelled

    # -- internals ---------------------------------------------------------------
    def _claim_key(self, key: str) -> Order | None:
        """In-progress or completed: the two states an idempotency record can be in."""
        with self._lock:
            record = self._checkouts.get(key)
            if record is None:
                self._checkouts[key] = CheckoutRecord(key, self._clock.now())
                return None
            order_id = record.order_id
        if order_id is None:
            raise CheckoutInProgressError(f"checkout {key} is already in flight")
        return self.orders.order(order_id)

    def _complete_key(self, key: str, order_id: str) -> None:
        with self._lock:
            self._checkouts[key].order_id = order_id

    def _abandon_key(self, key: str) -> None:
        with self._lock:
            self._checkouts.pop(key, None)  # a failed attempt may be retried

    def _safe_release(self, hold_id: str) -> None:
        try:
            self.inventory.release(hold_id)
        except HoldExpiredError:
            pass  # already released, expired or committed; nothing to give back

    def _restock(self, order: Order) -> None:
        for line in self.inventory.hold(order.hold_id).lines:
            self.inventory.add_stock(line.sku_id, line.warehouse_id, line.quantity)

    def _refund(self, order: Order) -> None:
        for payment in self.payments.all():
            if payment.order_id == order.id and payment.status is PaymentStatus.CAPTURED:
                payment.status = PaymentStatus.REFUNDED

    def _publish(self, topic: str, order: Order) -> None:
        payload = {
            "order_id": order.id,
            "customer_id": order.customer_id,
            "total": str(order.total),
            "sku_ids": ",".join(item.sku_id for item in order.items),
            "tracking": f"TRK-{order.id}",
        }
        self.bus.publish(Event(topic, self._clock.now(), payload))


# --8<-- [end:checkout]
