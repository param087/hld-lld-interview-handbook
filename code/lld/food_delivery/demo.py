"""One evening in one neighbourhood: order, offer, timeout cascade, delivery, rating."""

from decimal import Decimal

from common import FakeClock, Money, SequentialIdGenerator
from lld.food_delivery.facade import FoodDeliveryService
from lld.food_delivery.models import (
    Cart,
    DeliveryPartner,
    Location,
    Menu,
    MenuItem,
    PaymentMethod,
    Restaurant,
)
from lld.food_delivery.strategies import CouponBook, PercentOff

HOME = Location(12.9352, 77.6245)
START_EPOCH = 1_772_020_800.0  # 2026-02-25T12:00Z


def build_service(clock: FakeClock) -> FoodDeliveryService:
    menu = Menu()
    menu.add(MenuItem("m1", "paneer tikka", Money.of("6.50")))
    menu.add(MenuItem("m2", "garlic naan", Money.of("2.00")))
    menu.add(MenuItem("m3", "gulab jamun", Money.of("3.00"), available=False))
    kitchen = Restaurant("r1", "Curry Corner", Location(12.9380, 77.6260), prep_minutes=18, menu=menu)
    partners = [
        DeliveryPartner("p1", "Asha", Location(12.9375, 77.6255), rating=4.9, ratings_count=30),
        DeliveryPartner("p2", "Bala", Location(12.9400, 77.6300), rating=4.6, ratings_count=12),
        DeliveryPartner("p3", "Chetan", Location(12.9500, 77.6400), rating=4.8, ratings_count=4),
    ]
    coupons = CouponBook({"SAVE10": PercentOff(Decimal("0.10"), Money.of("3.00"))})
    service = FoodDeliveryService(
        [kitchen], partners, coupons=coupons, clock=clock, ids=SequentialIdGenerator("O"), offer_timeout=30.0
    )
    for partner in partners:
        service.delivery.go_online(partner.id)
    return service


def main() -> None:
    clock = FakeClock(start=START_EPOCH)
    service = build_service(clock)
    print(f"open near home: {[r.name for r in service.browse(HOME, radius_km=2.0)]}")

    cart = Cart("cust-1")
    cart.add("r1", "m1", 2)
    cart.add("r1", "m2", 3)
    order = service.place_order(cart, HOME, PaymentMethod.CARD, coupon_code="SAVE10")
    print(f"{order.id} placed: subtotal {order.subtotal}, SAVE10 -{order.discount}, fee {order.delivery_fee}, total {order.total}")

    offer = service.restaurant_accepts(order.id)
    assert offer is not None
    print(f"{order.id} accepted and cooking; {offer.id} offered to {offer.partner_id} until +30 s")

    clock.advance(31)
    expired = service.sweep_offers()
    print(f"{expired[0].id} expired ({expired[0].status}); {expired[0].partner_id} is idle again")
    second = service.delivery.offers_for(order.id)[-1]
    print(f"cascaded to {second.partner_id}, who declines")
    third = service.partner_declines(second.id, second.partner_id)
    assert third is not None
    print(f"cascaded again to {third.partner_id}, who accepts")
    service.partner_accepts(third.id, third.partner_id)

    service.mark_ready(order.id)
    service.pick_up(order.id)
    delivered = service.deliver(order.id)
    payment = service.payments.payment_for(order.id)
    assert payment is not None
    print(f"{delivered.id} {delivered.status} by {delivered.partner_id}; payment {payment.status} {payment.amount}")

    service.rate_delivery(order.id, 5, "fast")
    print(f"{third.partner_id} rating is now {service.delivery.partner(third.partner_id).rating}")
    for line in service.notifications.inbox("cust-1"):
        print(f"    customer: {line}")


if __name__ == "__main__":
    main()
