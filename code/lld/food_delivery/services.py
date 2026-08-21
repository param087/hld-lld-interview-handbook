"""Order, dispatch and payment services plus the facade that sequences them."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock, ValidationError
from lld.food_delivery.models import (
    Cart,
    DeliveryOffer,
    DeliveryPartner,
    ItemUnavailableError,
    Location,
    NoPartnerAvailableError,
    OfferStateError,
    OfferStatus,
    Order,
    OrderItem,
    OrderStateError,
    OrderStatus,
    PartnerStatus,
    Payment,
    PaymentDeclinedError,
    PaymentMethod,
    PaymentStatus,
    Restaurant,
    RestaurantClosedError,
    UnknownOrderError,
    UnknownPartnerError,
)
from lld.food_delivery.strategies import (
    AssignmentStrategy,
    CouponBook,
    GatewayFactory,
    NearestPartner,
    PaymentGateway,
)

DEFAULT_DELIVERY_FEE = Money.of("2.50")


# --8<-- [start:orders]
class OrderService:
    """Owns the order registry and *every* status change.

    One lock guards both. Each transition is a check-and-flip against
    ``ORDER_TRANSITIONS``: a caller can never observe an order half-way between
    two states, and two callers can never both win the same transition.
    """

    def __init__(
        self,
        restaurants: Iterable[Restaurant],
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        coupons: CouponBook | None = None,
        delivery_fee: Money = DEFAULT_DELIVERY_FEE,
    ) -> None:
        self._restaurants = {r.id: r for r in restaurants}
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("O")
        self._coupons = coupons or CouponBook()
        self._fee = delivery_fee
        self._orders: dict[str, Order] = {}
        self._lock = threading.Lock()

    def restaurant(self, restaurant_id: str) -> Restaurant:
        try:
            return self._restaurants[restaurant_id]
        except KeyError:
            raise UnknownOrderError(f"no restaurant {restaurant_id}") from None

    def nearby(self, location: Location, radius_km: float = 5.0) -> list[Restaurant]:
        open_ones = [r for r in self._restaurants.values() if r.is_open]
        within = [r for r in open_ones if location.distance_km(r.location) <= radius_km]
        return sorted(within, key=lambda r: (location.distance_km(r.location), r.id))

    def order(self, order_id: str) -> Order:
        with self._lock:
            try:
                return self._orders[order_id]
            except KeyError:
                raise UnknownOrderError(f"unknown order {order_id}") from None

    def place(self, cart: Cart, deliver_to: Location, coupon_code: str | None = None) -> Order:
        """Snapshot prices *now*: the menu may change while the food is cooking."""
        if cart.is_empty() or cart.restaurant_id is None:
            raise ValidationError("cannot place an empty cart")
        restaurant = self.restaurant(cart.restaurant_id)
        if not restaurant.is_open:
            raise RestaurantClosedError(f"{restaurant.name} is closed")
        items = []
        for item_id, quantity in cart.lines.items():
            menu_item = restaurant.menu.item(item_id)
            if not menu_item.available:
                raise ItemUnavailableError(f"{menu_item.name} is sold out")
            items.append(OrderItem(item_id, menu_item.name, menu_item.price, quantity))
        order = Order(
            id=self._ids.next_id(),
            customer_id=cart.customer_id,
            restaurant_id=restaurant.id,
            items=tuple(items),
            deliver_to=deliver_to,
            delivery_fee=self._fee,
            discount=Money(0),
            placed_at=self._clock.now(),
            coupon_code=coupon_code,
        )
        order.discount = self._coupons.lookup(coupon_code).discount(order.subtotal, self._fee)
        with self._lock:
            self._orders[order.id] = order
        return order

    def transition(self, order_id: str, target: OrderStatus) -> Order:
        """The only way an order changes state. Rejected moves raise, never no-op."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise UnknownOrderError(f"unknown order {order_id}")
            if not order.can_move_to(target):
                raise OrderStateError(f"order {order_id} cannot move {order.status} to {target}")
            order.status = target
            return order

    def attach_partner(self, order_id: str, partner_id: str) -> Order:
        """Pin a courier. Fails if the order was cancelled or already has one."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise UnknownOrderError(f"unknown order {order_id}")
            if not order.is_assignable():
                raise OrderStateError(f"order {order_id} is {order.status}, not assignable")
            if order.partner_id is not None:
                raise OrderStateError(f"order {order_id} already has courier {order.partner_id}")
            order.partner_id = partner_id
            return order


# --8<-- [end:orders]


# --8<-- [start:dispatch]
class DeliveryService:
    """Courier state, live offers and the lease. One lock over all three.

    The invariant: a courier is in exactly one of IDLE, OFFERED and DELIVERING,
    and at most one PENDING offer names them. ``offer`` flips IDLE to OFFERED
    inside the lock, so a courier that two dispatchers pick simultaneously is
    still only offered one order -- the second dispatcher finds nobody idle and
    moves down its ranking.
    """

    OFFER_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        partners: Iterable[DeliveryPartner],
        strategy: AssignmentStrategy | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        offer_timeout: float = OFFER_TIMEOUT_SECONDS,
    ) -> None:
        self._partners = {p.id: p for p in partners}
        self._strategy = strategy or NearestPartner()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("OF")
        self._timeout = offer_timeout
        self._offers: dict[str, DeliveryOffer] = {}
        self._passed_on: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def partner(self, partner_id: str) -> DeliveryPartner:
        try:
            return self._partners[partner_id]
        except KeyError:
            raise UnknownPartnerError(f"no partner {partner_id}") from None

    def go_online(self, partner_id: str, location: Location | None = None) -> DeliveryPartner:
        with self._lock:
            partner = self.partner(partner_id)
            if location is not None:
                partner.location = location
            partner.status = PartnerStatus.IDLE
            return partner

    def update_location(self, partner_id: str, location: Location) -> None:
        with self._lock:
            self.partner(partner_id).location = location

    def offer(self, order_id: str, origin: Location) -> DeliveryOffer:
        """Lease the best idle courier who has not already passed on this order."""
        with self._lock:
            now = self._clock.now()
            live = [o for o in self._offers.values() if o.order_id == order_id and o.is_live(now)]
            if live:
                return live[0]  # dispatch is idempotent: one live offer per order
            passed = self._passed_on.setdefault(order_id, set())
            free = [p for p in self._partners.values() if p.is_free() and p.id not in passed]
            ranked = self._strategy.rank(origin, free)
            if not ranked:
                raise NoPartnerAvailableError(f"no courier free for order {order_id}")
            partner = ranked[0]
            offer = DeliveryOffer(self._ids.next_id(), order_id, partner.id, now, now + self._timeout)
            partner.status = PartnerStatus.OFFERED
            partner.current_order_id = order_id
            self._offers[offer.id] = offer
            return offer

    def accept(self, offer_id: str, partner_id: str) -> DeliveryOffer:
        """Claim the lease. Exactly one caller can move an offer out of PENDING."""
        with self._lock:
            offer = self._offers.get(offer_id)
            if offer is None:
                raise OfferStateError(f"unknown offer {offer_id}")
            if offer.partner_id != partner_id:
                raise OfferStateError(f"offer {offer_id} belongs to {offer.partner_id}")
            if not offer.is_live(self._clock.now()):
                raise OfferStateError(f"offer {offer_id} is {offer.status} or expired")
            offer.status = OfferStatus.ACCEPTED
            partner = self.partner(partner_id)
            partner.status = PartnerStatus.DELIVERING
            return offer

    def decline(self, offer_id: str, partner_id: str) -> DeliveryOffer:
        with self._lock:
            offer = self._offers.get(offer_id)
            if offer is None or offer.partner_id != partner_id:
                raise OfferStateError(f"offer {offer_id} is not addressed to {partner_id}")
            if offer.status is not OfferStatus.PENDING:
                raise OfferStateError(f"offer {offer_id} is already {offer.status}")
            self._retire(offer, OfferStatus.DECLINED)
            return offer

    def sweep(self) -> list[DeliveryOffer]:
        """Expire every lease whose timeout has passed. Call it from a timer."""
        now = self._clock.now()
        with self._lock:
            stale = [o for o in self._offers.values() if o.status is OfferStatus.PENDING and now >= o.expires_at]
            for offer in stale:
                self._retire(offer, OfferStatus.EXPIRED)
            return stale

    def release(self, offer: DeliveryOffer) -> None:
        """Undo an accepted claim because the order moved under the courier."""
        with self._lock:
            self._retire(offer, OfferStatus.VOIDED, remember=False)

    def void_order(self, order_id: str) -> None:
        with self._lock:
            for offer in self._offers.values():
                if offer.order_id == order_id and offer.status in (OfferStatus.PENDING, OfferStatus.ACCEPTED):
                    self._retire(offer, OfferStatus.VOIDED, remember=False)
            self._passed_on.pop(order_id, None)

    def complete(self, partner_id: str) -> DeliveryPartner:
        with self._lock:
            partner = self.partner(partner_id)
            partner.status = PartnerStatus.IDLE
            partner.current_order_id = None
            partner.deliveries_today += 1
            return partner

    def offers_for(self, order_id: str) -> list[DeliveryOffer]:
        with self._lock:
            return [o for o in self._offers.values() if o.order_id == order_id]

    def _retire(self, offer: DeliveryOffer, status: OfferStatus, remember: bool = True) -> None:
        """Caller holds the lock. Frees the courier and optionally records the pass."""
        offer.status = status
        partner = self._partners.get(offer.partner_id)
        # Free the courier only if they are still holding *this* order. Retiring a
        # stale offer must never take a courier off the delivery they have since
        # accepted -- the classic "release a resource someone else now owns" bug.
        still_held = partner is not None and partner.current_order_id == offer.order_id
        if still_held and partner is not None and partner.status is not PartnerStatus.OFFLINE:
            partner.status = PartnerStatus.IDLE
            partner.current_order_id = None
        if remember:
            self._passed_on.setdefault(offer.order_id, set()).add(offer.partner_id)


# --8<-- [end:dispatch]


class PaymentService:
    """Authorize at checkout, capture on delivery, void when nothing was delivered."""

    def __init__(self, clock: Clock | None = None, ids: IdGenerator | None = None) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("PAY")
        self._payments: dict[str, Payment] = {}
        self._lock = threading.Lock()

    def authorize(self, order: Order, method: PaymentMethod, gateway: PaymentGateway | None = None) -> Payment:
        gateway = gateway or GatewayFactory.for_method(method)
        if not gateway.authorize(order.total):
            raise PaymentDeclinedError(f"{method} authorization of {order.total} declined")
        payment = Payment(self._ids.next_id(), order.id, order.total, PaymentMethod(method))
        with self._lock:
            self._payments[order.id] = payment
        return payment

    def payment_for(self, order_id: str) -> Payment | None:
        with self._lock:
            return self._payments.get(order_id)

    def capture(self, order_id: str) -> Payment:
        return self._settle(order_id, PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED)

    def void(self, order_id: str) -> Payment | None:
        payment = self.payment_for(order_id)
        if payment is None or payment.status is not PaymentStatus.AUTHORIZED:
            return None
        return self._settle(order_id, PaymentStatus.AUTHORIZED, PaymentStatus.VOIDED)

    def refund(self, order_id: str) -> Payment:
        return self._settle(order_id, PaymentStatus.CAPTURED, PaymentStatus.REFUNDED)

    def _settle(self, order_id: str, expected: PaymentStatus, target: PaymentStatus) -> Payment:
        with self._lock:
            payment = self._payments.get(order_id)
            if payment is None:
                raise UnknownOrderError(f"no payment for order {order_id}")
            if payment.status is not expected:
                raise OrderStateError(f"payment for {order_id} is {payment.status}, expected {expected}")
            payment.status = target
            return payment
