from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, Money, SequentialIdGenerator, ValidationError
from lld.food_delivery.facade import FoodDeliveryService
from lld.food_delivery.messaging import EventBus
from lld.food_delivery.models import (
    Cart,
    DeliveryPartner,
    Event,
    ItemUnavailableError,
    Location,
    Menu,
    MenuItem,
    OfferStateError,
    OfferStatus,
    OrderStateError,
    OrderStatus,
    PartnerStatus,
    PaymentMethod,
    PaymentStatus,
    Restaurant,
    RestaurantClosedError,
)
from lld.food_delivery.strategies import (
    AssignmentStrategy,
    BestRatedNearby,
    CouponBook,
    FairRotation,
    FlatOff,
    NearestPartner,
)

START_EPOCH = 1_772_020_800.0  # 2026-02-25T12:00Z
HOME = Location(12.9352, 77.6245)
KITCHEN = Location(12.9380, 77.6260)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START_EPOCH)


def build(clock: FakeClock, couriers: int = 3, offer_timeout: float = 30.0) -> FoodDeliveryService:
    menu = Menu()
    menu.add(MenuItem("m1", "paneer tikka", Money.of("6.50")))
    menu.add(MenuItem("m2", "garlic naan", Money.of("2.00")))
    menu.add(MenuItem("m3", "gulab jamun", Money.of("3.00"), available=False))
    restaurant = Restaurant("r1", "Curry Corner", KITCHEN, menu=menu)
    partners = [DeliveryPartner(f"p{i}", f"courier {i}", KITCHEN) for i in range(1, couriers + 1)]
    coupons = CouponBook({"FLAT5": FlatOff(Money.of("5.00"), Money.of("10.00"))})
    service = FoodDeliveryService(
        [restaurant], partners, coupons=coupons, clock=clock,
        ids=SequentialIdGenerator("O"), offer_timeout=offer_timeout,
    )
    for partner in partners:
        service.delivery.go_online(partner.id)
    return service


def full_cart(customer: str = "cust-1") -> Cart:
    cart = Cart(customer)
    cart.add("r1", "m1", 2)  # 13.00
    cart.add("r1", "m2", 1)  # 2.00
    return cart


def test_place_order_snapshots_prices_and_applies_the_coupon(clock: FakeClock) -> None:
    service = build(clock)
    order = service.place_order(full_cart(), HOME, PaymentMethod.CARD, coupon_code="FLAT5")
    assert order.subtotal == Money.of("15.00") and order.discount == Money.of("5.00")
    assert order.total == Money.of("12.50")  # 15.00 - 5.00 + 2.50 delivery
    # The menu changes afterwards; the order keeps the price the customer agreed to.
    service.orders.restaurant("r1").menu.item("m1").price = Money.of("9.99")
    assert order.subtotal == Money.of("15.00")
    assert service.payments.payment_for(order.id).status is PaymentStatus.AUTHORIZED


def test_sold_out_items_empty_carts_and_closed_kitchens_are_rejected(clock: FakeClock) -> None:
    service = build(clock)
    sold_out = Cart("cust-1")
    sold_out.add("r1", "m3", 1)
    with pytest.raises(ItemUnavailableError):
        service.place_order(sold_out, HOME, PaymentMethod.CARD)
    with pytest.raises(ValidationError):
        service.place_order(Cart("cust-1"), HOME, PaymentMethod.CARD)
    service.orders.restaurant("r1").is_open = False
    with pytest.raises(RestaurantClosedError):
        service.place_order(full_cart(), HOME, PaymentMethod.CARD)


@pytest.mark.parametrize(
    ("target", "legal"),
    [
        (OrderStatus.ACCEPTED, True),
        (OrderStatus.REJECTED, True),
        (OrderStatus.CANCELLED, True),
        (OrderStatus.READY, False),
        (OrderStatus.DELIVERED, False),
        (OrderStatus.PICKED_UP, False),
    ],
)
def test_the_transition_table_is_the_only_gate(clock: FakeClock, target: OrderStatus, legal: bool) -> None:
    service = build(clock)
    order = service.place_order(full_cart(), HOME, PaymentMethod.CARD)
    if legal:
        assert service.orders.transition(order.id, target).status is target
    else:
        with pytest.raises(OrderStateError):
            service.orders.transition(order.id, target)


def test_offer_leases_a_courier_and_dispatch_is_idempotent(clock: FakeClock) -> None:
    service = build(clock, couriers=1)
    order = service.place_order(full_cart(), HOME, PaymentMethod.CARD)
    offer = service.restaurant_accepts(order.id)
    assert offer is not None and service.delivery.partner("p1").status is PartnerStatus.OFFERED
    assert service.dispatch(order.id) is offer  # a retry does not double-offer
    second = service.place_order(full_cart("cust-2"), HOME, PaymentMethod.CARD)
    assert service.restaurant_accepts(second.id) is None  # the only courier is leased


# --8<-- [start:double_assign]
def test_concurrent_dispatch_never_offers_one_courier_two_orders(clock: FakeClock) -> None:
    service = build(clock, couriers=3)
    orders = [service.place_order(full_cart(f"c{i}"), HOME, PaymentMethod.CARD) for i in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        offers = [o for o in pool.map(lambda o: service.restaurant_accepts(o.id), orders) if o is not None]

    leased = [o.partner_id for o in offers]
    assert len(leased) == 3 and len(set(leased)) == 3  # three couriers, three leases, no overlap
    assert all(service.delivery.partner(p).status is PartnerStatus.OFFERED for p in leased)
    assert sum(1 for o in orders if o.partner_id is not None) == 0  # a lease is not an assignment


def test_only_one_thread_can_accept_the_same_offer(clock: FakeClock) -> None:
    service = build(clock, couriers=1)
    order = service.place_order(full_cart(), HOME, PaymentMethod.CARD)
    offer = service.restaurant_accepts(order.id)
    assert offer is not None

    def accept(_: int) -> bool:
        try:
            service.partner_accepts(offer.id, "p1")
        except OfferStateError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(accept, range(12)))
    assert results.count(True) == 1 and order.partner_id == "p1"


# --8<-- [end:double_assign]


# --8<-- [start:cancel_race]
def test_cancel_racing_accept_never_strands_a_courier(clock: FakeClock) -> None:
    """Both calls may succeed; what must never happen is a courier left on a dead order."""
    service = build(clock, couriers=20)
    pairs = []
    for i in range(20):
        order = service.place_order(full_cart(f"c{i}"), HOME, PaymentMethod.CARD)
        offer = service.restaurant_accepts(order.id)
        assert offer is not None
        pairs.append((order, offer))

    def run(task: tuple) -> None:
        action, order, offer = task
        try:
            if action == "accept":
                service.partner_accepts(offer.id, offer.partner_id)
            else:
                service.cancel_order(order.id)
        except (OfferStateError, OrderStateError):
            pass

    tasks = [(a, order, offer) for order, offer in pairs for a in ("accept", "cancel")]
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(run, tasks))

    for order, offer in pairs:
        partner = service.delivery.partner(offer.partner_id)
        if order.status is OrderStatus.CANCELLED:
            assert partner.status is PartnerStatus.IDLE and partner.current_order_id is None
        else:
            assert order.partner_id == partner.id and partner.status is PartnerStatus.DELIVERING


# --8<-- [end:cancel_race]


def test_offer_timeout_cascades_to_the_next_courier(clock: FakeClock) -> None:
    service = build(clock, couriers=2, offer_timeout=30.0)
    order = service.place_order(full_cart(), HOME, PaymentMethod.CARD)
    first = service.restaurant_accepts(order.id)
    assert first is not None

    clock.advance(31)
    expired = service.sweep_offers()
    assert [o.status for o in expired] == [OfferStatus.EXPIRED]
    assert service.delivery.partner(first.partner_id).status is PartnerStatus.IDLE
    live = [o for o in service.delivery.offers_for(order.id) if o.status is OfferStatus.PENDING]
    assert len(live) == 1 and live[0].partner_id != first.partner_id  # never re-offered to the same courier
    with pytest.raises(OfferStateError):
        service.partner_accepts(first.id, first.partner_id)  # the stale offer is dead


def test_declining_walks_down_the_ranking_until_nobody_is_left(clock: FakeClock) -> None:
    service = build(clock, couriers=2)
    order = service.place_order(full_cart(), HOME, PaymentMethod.CARD)
    first = service.restaurant_accepts(order.id)
    assert first is not None
    second = service.partner_declines(first.id, first.partner_id)
    assert second is not None and second.partner_id != first.partner_id
    assert service.partner_declines(second.id, second.partner_id) is None  # cascade exhausted


def test_payment_is_authorized_then_captured_and_voided_when_rejected(clock: FakeClock) -> None:
    service = build(clock)
    happy = service.place_order(full_cart(), HOME, PaymentMethod.CARD)
    offer = service.restaurant_accepts(happy.id)
    assert offer is not None
    service.partner_accepts(offer.id, offer.partner_id)
    service.mark_ready(happy.id)
    service.pick_up(happy.id)
    service.deliver(happy.id)
    assert service.payments.payment_for(happy.id).status is PaymentStatus.CAPTURED
    assert service.delivery.partner(offer.partner_id).deliveries_today == 1

    refused = service.place_order(full_cart("cust-2"), HOME, PaymentMethod.WALLET)
    service.restaurant_rejects(refused.id)
    assert service.payments.payment_for(refused.id).status is PaymentStatus.VOIDED


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (NearestPartner(), "near"),
        (BestRatedNearby(radius_km=3.0), "star"),
        (FairRotation(radius_km=5.0), "fresh"),
    ],
)
def test_assignment_strategies_pick_different_couriers(strategy: AssignmentStrategy, expected: str) -> None:
    candidates = [
        DeliveryPartner("near", "closest", Location(12.9381, 77.6261), rating=4.1, deliveries_today=9),
        DeliveryPartner("star", "best rated", Location(12.9400, 77.6280), rating=5.0, deliveries_today=7),
        DeliveryPartner("fresh", "least busy", Location(12.9410, 77.6290), rating=4.4, deliveries_today=0),
    ]
    assert strategy.rank(KITCHEN, candidates)[0].id == expected


def test_event_bus_isolates_a_failing_handler() -> None:
    bus, seen = EventBus(), []
    bus.subscribe("order.placed", lambda e: (_ for _ in ()).throw(RuntimeError("push service down")))
    bus.subscribe("order.placed", seen.append)
    assert bus.publish(Event("order.placed", 0.0, {"order_id": "O-1"})) == 1
    assert len(seen) == 1 and bus.failures() == [("order.placed", "push service down")]
