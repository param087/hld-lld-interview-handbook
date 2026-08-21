"""The orchestration layer: one object the API holds, sequencing the three services."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.food_delivery.messaging import EventBus, NotificationService
from lld.food_delivery.models import (
    Cart,
    DeliveryOffer,
    DeliveryPartner,
    Event,
    Location,
    NoPartnerAvailableError,
    Order,
    OrderStateError,
    OrderStatus,
    PaymentDeclinedError,
    PaymentMethod,
    Rating,
    Restaurant,
)
from lld.food_delivery.services import DeliveryService, OrderService, PaymentService
from lld.food_delivery.strategies import AssignmentStrategy, CouponBook


# --8<-- [start:facade]
class FoodDeliveryService:
    """The one object the API layer holds. It sequences, it does not compute.

    Every step is *claim, then act, then revert on failure*: the offer lease is
    claimed in ``DeliveryService`` and the order is pinned in ``OrderService``,
    and if the second call fails the first is undone. No method ever holds two
    service locks at once, so there is no lock order to get wrong.
    """

    def __init__(
        self,
        restaurants: Iterable[Restaurant],
        partners: Iterable[DeliveryPartner],
        strategy: AssignmentStrategy | None = None,
        coupons: CouponBook | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        offer_timeout: float = DeliveryService.OFFER_TIMEOUT_SECONDS,
    ) -> None:
        clock = clock or SystemClock()
        self._clock = clock
        self.bus = EventBus()
        self.notifications = NotificationService(self.bus)
        self.orders = OrderService(restaurants, clock=clock, ids=ids or SequentialIdGenerator("O"), coupons=coupons)
        self.delivery = DeliveryService(partners, strategy, clock=clock, offer_timeout=offer_timeout)
        self.payments = PaymentService(clock=clock)
        self._ratings: list[Rating] = []
        self._lock = threading.Lock()

    # -- customer ---------------------------------------------------------------
    def browse(self, location: Location, radius_km: float = 5.0) -> list[Restaurant]:
        return self.orders.nearby(location, radius_km)

    def place_order(
        self, cart: Cart, deliver_to: Location, method: PaymentMethod, coupon_code: str | None = None
    ) -> Order:
        order = self.orders.place(cart, deliver_to, coupon_code)
        try:
            self.payments.authorize(order, method)
        except PaymentDeclinedError:
            self.orders.transition(order.id, OrderStatus.CANCELLED)
            raise
        self._publish("order.placed", order)
        return order

    def cancel_order(self, order_id: str) -> Order:
        """Flip the order first: a courier accepting at the same instant now loses."""
        order = self.orders.transition(order_id, OrderStatus.CANCELLED)
        self.delivery.void_order(order_id)
        self.payments.void(order_id)
        self._publish("order.cancelled", order)
        return order

    def rate_delivery(self, order_id: str, stars: int, comment: str = "") -> Rating:
        order = self.orders.order(order_id)
        if order.status is not OrderStatus.DELIVERED or order.partner_id is None:
            raise OrderStateError(f"order {order_id} was not delivered")
        rating = Rating(order_id, order.partner_id, stars, comment)
        partner = self.delivery.partner(order.partner_id)
        with self._lock:
            self._ratings.append(rating)
            total = partner.rating * partner.ratings_count + stars
            partner.ratings_count += 1
            partner.rating = round(total / partner.ratings_count, 2)
        return rating

    # -- restaurant -------------------------------------------------------------
    def restaurant_accepts(self, order_id: str) -> DeliveryOffer | None:
        order = self.orders.transition(order_id, OrderStatus.ACCEPTED)
        self._publish("order.accepted", order)
        self.orders.transition(order_id, OrderStatus.PREPARING)
        return self.dispatch(order_id)  # dispatch while the food cooks

    def restaurant_rejects(self, order_id: str) -> Order:
        order = self.orders.transition(order_id, OrderStatus.REJECTED)
        self.payments.void(order_id)
        self._publish("order.rejected", order)
        return order

    def mark_ready(self, order_id: str) -> Order:
        order = self.orders.transition(order_id, OrderStatus.READY)
        self._publish("order.ready", order)
        return order

    # -- dispatch ---------------------------------------------------------------
    def dispatch(self, order_id: str) -> DeliveryOffer | None:
        """Offer the order to the next-best courier. None means nobody is left."""
        order = self.orders.order(order_id)
        if not order.is_assignable() or order.partner_id is not None:
            return None
        try:
            return self.delivery.offer(order_id, self.orders.restaurant(order.restaurant_id).location)
        except NoPartnerAvailableError:
            return None

    def partner_accepts(self, offer_id: str, partner_id: str) -> Order:
        offer = self.delivery.accept(offer_id, partner_id)  # claim the lease
        try:
            order = self.orders.attach_partner(offer.order_id, partner_id)
        except OrderStateError:
            self.delivery.release(offer)  # the order was cancelled under the courier
            raise
        self._publish("order.assigned", order)
        return order

    def partner_declines(self, offer_id: str, partner_id: str) -> DeliveryOffer | None:
        offer = self.delivery.decline(offer_id, partner_id)
        return self.dispatch(offer.order_id)

    def sweep_offers(self) -> list[DeliveryOffer]:
        """Expire stale leases and cascade each order to the next courier."""
        expired = self.delivery.sweep()
        for offer in expired:
            self.dispatch(offer.order_id)
        return expired

    def pick_up(self, order_id: str) -> Order:
        order = self.orders.order(order_id)
        if order.partner_id is None:
            raise OrderStateError(f"order {order_id} has no courier")
        order = self.orders.transition(order_id, OrderStatus.PICKED_UP)
        self._publish("order.picked_up", order)
        return order

    def deliver(self, order_id: str) -> Order:
        order = self.orders.transition(order_id, OrderStatus.DELIVERED)
        self.payments.capture(order_id)
        if order.partner_id:
            self.delivery.complete(order.partner_id)
        self._publish("order.delivered", order)
        return order

    def _publish(self, topic: str, order: Order) -> None:
        payload = {
            "order_id": order.id,
            "customer_id": order.customer_id,
            "restaurant": order.restaurant_id,
            "total": str(order.total),
        }
        if order.partner_id:
            payload["partner"] = order.partner_id
        self.bus.publish(Event(topic, self._clock.now(), payload))


# --8<-- [end:facade]
