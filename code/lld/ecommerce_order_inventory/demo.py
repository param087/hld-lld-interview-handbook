"""A flash sale in miniature: search, hold, pay, retry, oversell, abandoned cart."""

from decimal import Decimal

from common import FakeClock, Money, SequentialIdGenerator
from lld.ecommerce_order_inventory.checkout import CheckoutFacade
from lld.ecommerce_order_inventory.events import EventBus, LowStockMonitor, NotificationService
from lld.ecommerce_order_inventory.inventory import InventoryService
from lld.ecommerce_order_inventory.models import (
    Address,
    Category,
    OutOfStockError,
    Product,
    ShippingSpeed,
    Sku,
    Warehouse,
)
from lld.ecommerce_order_inventory.services import (
    CartService,
    CatalogService,
    OrderService,
    ShipmentDispatcher,
)
from lld.ecommerce_order_inventory.strategies import InStock, PercentOff, PriceBelow, RegionTax

START_EPOCH = 1_772_020_800.0  # 2026-02-25T12:00Z
SHIP_TO = Address("12 Long Acre", "London", "WC2E 9LG", "GB", region="uk")
EAST = Warehouse("w-east", "East", "uk")
WEST = Warehouse("w-west", "West", "uk")


def build(clock: FakeClock) -> CheckoutFacade:
    inventory = InventoryService([EAST, WEST], clock=clock, ids=SequentialIdGenerator("H"), hold_ttl=900.0)
    catalog = CatalogService(
        inventory,
        [Category("c-kitchen", "Kitchen")],
        [Product(f"p-{name}", name.title(), "c-kitchen") for name in ("kettle", "mug", "grinder")],
        [
            Sku("sku-kettle", "p-kettle", Money.of("39.00"), (("colour", "black"),)),
            Sku("sku-mug", "p-mug", Money.of("9.50")),
            Sku("sku-grinder", "p-grinder", Money.of("119.00")),
        ],
    )
    inventory.add_stock("sku-kettle", EAST.id, 2)
    inventory.add_stock("sku-kettle", WEST.id, 3)
    inventory.add_stock("sku-mug", EAST.id, 40)
    inventory.add_stock("sku-grinder", WEST.id, 1)
    return CheckoutFacade(
        catalog, CartService(SequentialIdGenerator("CART")), inventory,
        OrderService(clock=clock, ids=SequentialIdGenerator("ORD")), EventBus(),
        discount=PercentOff(Decimal("0.10"), Money.of("15.00"), Money.of("50.00")),
        tax=RegionTax({"uk": Decimal("0.20")}), clock=clock, ids=SequentialIdGenerator("PAY"),
    )


def main() -> None:
    clock = FakeClock(start=START_EPOCH)
    shop = build(clock)
    notifications = NotificationService(shop.bus)
    low_stock = LowStockMonitor(shop.bus, shop.inventory, threshold=2)
    shipments = ShipmentDispatcher(shop.bus, shop.orders, SequentialIdGenerator("SHP"))

    in_stock_and_cheap = InStock(1) & PriceBelow(Money.of("50.00"))
    print(f"search kitchen, in stock, under 50.00: {[s.id for s in shop.catalog.search(in_stock_and_cheap, 'c-kitchen')]}")
    print(f"kettle: {shop.inventory.available('sku-kettle')} available across two warehouses")

    cart = shop.carts.open("cust-1")
    cart.add("sku-kettle", 4)
    cart.add("sku-mug", 2)
    order = shop.checkout(cart.id, "cust-1", SHIP_TO, "idem-1", ShippingSpeed.STANDARD)
    print(f"{order.id} {order.status}: {order.subtotal} - {order.discount} + {order.tax} tax + {order.shipping} ship = {order.total}")
    allocation = [
        f"{line.quantity}x{line.sku_id.removeprefix('sku-')}@{line.warehouse_id}"
        for line in shop.inventory.hold(order.hold_id).lines
    ]
    print(f"held {allocation}; kettles now {shop.inventory.available('sku-kettle')} available, {shop.inventory.reserved('sku-kettle')} reserved")
    print(f"retrying with key idem-1 returns {shop.checkout(cart.id, 'cust-1', SHIP_TO, 'idem-1').id} again, not a second order")

    rival = shop.carts.open("cust-2")
    rival.add("sku-kettle", 2)
    try:
        shop.checkout(rival.id, "cust-2", SHIP_TO, "idem-2")
    except OutOfStockError as exc:
        print(f"second buyer rejected: {exc}")
    print(f"low-stock alerts raised on order.placed: {low_stock.alerts()}")

    payment = shop.pay(order.id)
    print(f"{payment.id} {payment.status} {payment.amount}; kettles on hand {shop.inventory.on_hand('sku-kettle')}")

    abandoned = shop.carts.open("cust-3")
    abandoned.add("sku-grinder", 1)
    shop.checkout(abandoned.id, "cust-3", SHIP_TO, "idem-3")
    clock.advance(901)
    print(f"after the 15 min TTL the sweeper cancels {shop.sweep_expired_holds()}, grinder back to {shop.inventory.available('sku-grinder')}")

    shop.pack(order.id)
    shipped = shop.ship(order.id)
    print(f"{shipped.id} {shipped.status} via {shipments.shipment_for(order.id).tracking}")
    for line in notifications.inbox("cust-1"):
        print(f"    cust-1: {line}")


if __name__ == "__main__":
    main()
