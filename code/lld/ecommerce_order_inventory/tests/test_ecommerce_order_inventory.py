from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from common import ConflictError, FakeClock, Money, SequentialIdGenerator, ValidationError
from lld.ecommerce_order_inventory.checkout import CheckoutFacade
from lld.ecommerce_order_inventory.events import (
    ORDER_PLACED,
    EventBus,
    LowStockMonitor,
    NotificationService,
)
from lld.ecommerce_order_inventory.inventory import InventoryService
from lld.ecommerce_order_inventory.models import (
    Address,
    Category,
    CheckoutInProgressError,
    HoldExpiredError,
    HoldStatus,
    OrderStateError,
    OrderStatus,
    OutOfStockError,
    PaymentDeclinedError,
    PaymentStatus,
    PriceChangedError,
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
from lld.ecommerce_order_inventory.strategies import (
    ApprovingGateway,
    CheapestFreeInBundle,
    DecliningGateway,
    DiscountStrategy,
    HasAttribute,
    InStock,
    NoDiscount,
    PercentOff,
    PriceBelow,
    RegionTax,
    WeightBandShipping,
)

START_EPOCH = 1_772_020_800.0  # 2026-02-25T12:00Z
SHIP_TO = Address("12 Long Acre", "London", "WC2E 9LG", "GB", region="uk")
EAST = Warehouse("w-east", "East", "uk")
WEST = Warehouse("w-west", "West", "uk")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START_EPOCH)


def build(
    clock: FakeClock,
    stock: dict[tuple[str, str], int] | None = None,
    gateway: object | None = None,
    discount: DiscountStrategy | None = None,
    hold_ttl: float = 900.0,
) -> CheckoutFacade:
    inventory = InventoryService([EAST, WEST], clock=clock, ids=SequentialIdGenerator("H"), hold_ttl=hold_ttl)
    skus = [
        Sku("sku-kettle", "p-kettle", Money.of("39.00"), (("colour", "black"),)),
        Sku("sku-mug", "p-mug", Money.of("9.50"), (("colour", "white"),)),
        Sku("sku-grinder", "p-grinder", Money.of("119.00")),
    ]
    catalog = CatalogService(
        inventory,
        [Category("c-kitchen", "Kitchen")],
        [Product(f"p-{n}", n.title(), "c-kitchen") for n in ("kettle", "mug", "grinder")],
        skus,
    )
    for (sku_id, warehouse_id), quantity in (stock or {("sku-kettle", EAST.id): 5, ("sku-mug", EAST.id): 40}).items():
        inventory.add_stock(sku_id, warehouse_id, quantity)
    return CheckoutFacade(
        catalog, CartService(SequentialIdGenerator("CART")), inventory,
        OrderService(clock=clock, ids=SequentialIdGenerator("ORD")), EventBus(),
        discount=discount or NoDiscount(), tax=RegionTax({"uk": Decimal("0.20")}),
        shipping=WeightBandShipping(), gateway=gateway or ApprovingGateway(),
        clock=clock, ids=SequentialIdGenerator("PAY"),
    )


def cart_with(shop: CheckoutFacade, customer: str, **lines: int) -> str:
    cart = shop.carts.open(customer)
    for sku, quantity in lines.items():
        cart.add(f"sku-{sku}", quantity)
    return cart.id


def test_checkout_snapshots_prices_holds_stock_and_pays(clock: FakeClock) -> None:
    shop = build(clock, discount=PercentOff(Decimal("0.10"), Money.of("15.00"), Money.of("50.00")))
    cart_id = cart_with(shop, "cust-1", kettle=2, mug=1)
    order = shop.checkout(cart_id, "cust-1", SHIP_TO, "k1", ShippingSpeed.STANDARD)
    # 2 x 39.00 + 9.50 = 87.50; 10% off capped at 15.00 -> 8.75; tax 20% of 78.75 -> 15.75; free shipping over 50.
    assert order.subtotal == Money.of("87.50") and order.discount == Money.of("8.75")
    assert order.tax == Money.of("15.75") and order.shipping == Money(0)
    assert order.total == Money.of("94.50") and order.status is OrderStatus.CREATED
    assert shop.inventory.available("sku-kettle") == 3 and shop.inventory.reserved("sku-kettle") == 2

    shop.catalog.reprice("sku-kettle", Money.of("59.00"))  # the catalog moves; the order does not
    assert order.subtotal == Money.of("87.50")

    payment = shop.pay(order.id)
    assert payment.status is PaymentStatus.CAPTURED and order.status is OrderStatus.PAID
    assert shop.inventory.on_hand("sku-kettle") == 3  # the units left the building


# --8<-- [start:oversell]
def test_forty_buyers_race_for_twelve_units_and_exactly_twelve_win(clock: FakeClock) -> None:
    shop = build(clock, stock={("sku-kettle", EAST.id): 12})

    def buy(i: int) -> bool:
        cart_id = cart_with(shop, f"cust-{i}", kettle=1)
        try:
            shop.checkout(cart_id, f"cust-{i}", SHIP_TO, f"key-{i}")
        except OutOfStockError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(buy, range(40)))

    assert results.count(True) == 12  # never 13, never 11
    assert shop.inventory.available("sku-kettle") == 0
    assert shop.inventory.reserved("sku-kettle") == 12
    assert shop.inventory.on_hand("sku-kettle") == 12  # nothing was created or destroyed


def test_concurrent_duplicate_keys_produce_exactly_one_order(clock: FakeClock) -> None:
    shop = build(clock)
    cart_id = cart_with(shop, "cust-1", kettle=1)

    def submit(_: int) -> str | None:
        try:
            return shop.checkout(cart_id, "cust-1", SHIP_TO, "same-key").id
        except CheckoutInProgressError:
            return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(pool.map(submit, range(10)))

    created = [i for i in ids if i is not None]
    assert len(set(created)) == 1  # every caller that got an answer got the same order
    assert len(shop.orders.repository.all()) == 1
    assert shop.inventory.reserved("sku-kettle") == 1  # one hold, not ten


# --8<-- [end:oversell]


def test_reserve_is_all_or_nothing_across_skus(clock: FakeClock) -> None:
    shop = build(clock, stock={("sku-kettle", EAST.id): 5, ("sku-mug", EAST.id): 1})
    cart_id = cart_with(shop, "cust-1", kettle=2, mug=3)
    with pytest.raises(OutOfStockError):
        shop.checkout(cart_id, "cust-1", SHIP_TO, "k1")
    # The kettles were never touched: a partly reserved basket must not exist.
    assert shop.inventory.available("sku-kettle") == 5 and shop.inventory.reserved("sku-kettle") == 0
    assert shop.orders.repository.all() == []
    assert shop.checkout(cart_with(shop, "c2", kettle=1), "c2", SHIP_TO, "k1")  # the key is reusable


def test_one_sku_is_split_across_warehouses(clock: FakeClock) -> None:
    shop = build(clock, stock={("sku-kettle", EAST.id): 2, ("sku-kettle", WEST.id): 3})
    order = shop.checkout(cart_with(shop, "cust-1", kettle=4), "cust-1", SHIP_TO, "k1")
    lines = sorted((line.warehouse_id, line.quantity) for line in shop.inventory.hold(order.hold_id).lines)
    assert lines == [(EAST.id, 2), (WEST.id, 2)]
    assert shop.inventory.available("sku-kettle") == 1


def test_hold_ttl_expires_and_the_sweeper_cancels_the_order(clock: FakeClock) -> None:
    shop = build(clock, hold_ttl=60.0)
    order = shop.checkout(cart_with(shop, "cust-1", kettle=2), "cust-1", SHIP_TO, "k1")
    assert shop.inventory.available("sku-kettle") == 3
    clock.advance(61)
    assert shop.sweep_expired_holds() == [order.id]
    assert order.status is OrderStatus.CANCELLED
    assert shop.inventory.available("sku-kettle") == 5  # the units went back on the shelf
    assert shop.inventory.hold(order.hold_id).status is HoldStatus.EXPIRED


def test_paying_after_the_hold_expired_fails_and_cancels_cleanly(clock: FakeClock) -> None:
    shop = build(clock, hold_ttl=60.0)
    order = shop.checkout(cart_with(shop, "cust-1", kettle=2), "cust-1", SHIP_TO, "k1")
    clock.advance(61)  # nobody swept yet, but the deadline has passed
    with pytest.raises(HoldExpiredError):
        shop.pay(order.id)
    assert order.status is OrderStatus.CANCELLED and shop.inventory.available("sku-kettle") == 5
    assert shop.payments.all() == []  # the unit of work rolled the payment row back


def test_declined_card_releases_the_hold_and_leaves_no_payment(clock: FakeClock) -> None:
    shop = build(clock, gateway=DecliningGateway())
    order = shop.checkout(cart_with(shop, "cust-1", kettle=2), "cust-1", SHIP_TO, "k1")
    with pytest.raises(PaymentDeclinedError):
        shop.pay(order.id)
    assert order.status is OrderStatus.CANCELLED
    assert shop.inventory.available("sku-kettle") == 5 and shop.inventory.reserved("sku-kettle") == 0
    assert shop.payments.all() == []


@pytest.mark.parametrize(
    ("target", "legal"),
    [
        (OrderStatus.PAID, True),
        (OrderStatus.CANCELLED, True),
        (OrderStatus.PACKED, False),
        (OrderStatus.SHIPPED, False),
        (OrderStatus.DELIVERED, False),
        (OrderStatus.RETURNED, False),
    ],
)
def test_the_transition_table_is_the_only_gate(clock: FakeClock, target: OrderStatus, legal: bool) -> None:
    shop = build(clock)
    order = shop.checkout(cart_with(shop, "cust-1", kettle=1), "cust-1", SHIP_TO, "k1")
    if legal:
        assert shop.orders.transition(order.id, target).status is target
    else:
        with pytest.raises(OrderStateError):
            shop.orders.transition(order.id, target)


def test_return_after_delivery_restocks_and_refunds(clock: FakeClock) -> None:
    shop = build(clock)
    order = shop.checkout(cart_with(shop, "cust-1", kettle=2), "cust-1", SHIP_TO, "k1")
    shop.pay(order.id)
    assert shop.inventory.on_hand("sku-kettle") == 3
    for step in (shop.pack, shop.ship, shop.deliver):
        step(order.id)
    shop.accept_return(order.id)
    assert order.status is OrderStatus.RETURNED and shop.inventory.available("sku-kettle") == 5
    assert [p.status for p in shop.payments.all()] == [PaymentStatus.REFUNDED]


def test_price_drift_between_cart_and_checkout_is_rejected(clock: FakeClock) -> None:
    shop = build(clock)
    cart_id = cart_with(shop, "cust-1", kettle=1)
    shown = Money.of("46.80")  # 39.00 + 20% tax, free shipping does not apply below 50
    shop.catalog.reprice("sku-kettle", Money.of("49.00"))
    with pytest.raises(PriceChangedError):
        shop.checkout(cart_id, "cust-1", SHIP_TO, "k1", expected_total=shown)
    assert shop.inventory.reserved("sku-kettle") == 0  # nothing was held for a rejected basket


def test_guest_cart_merges_into_the_customer_cart(clock: FakeClock) -> None:
    shop = build(clock)
    mine = shop.carts.open("cust-1")
    mine.add("sku-kettle", 1)
    guest = shop.carts.open()
    guest.add("sku-kettle", 2)
    guest.add("sku-mug", 1)
    merged = shop.carts.merge_guest_cart(guest.id, "cust-1")
    assert merged.id == mine.id and merged.lines == {"sku-kettle": 3, "sku-mug": 1}
    assert guest.lines == {}
    with pytest.raises(ValidationError):
        merged.add("sku-mug", 0)


def test_specifications_compose_into_a_catalog_filter(clock: FakeClock) -> None:
    shop = build(clock, stock={("sku-kettle", EAST.id): 5})  # no mug rows at all
    cheap_and_stocked = InStock(1) & PriceBelow(Money.of("50.00"))
    assert [s.id for s in shop.catalog.search(cheap_and_stocked, "c-kitchen")] == ["sku-kettle"]
    assert [s.id for s in shop.catalog.search(~InStock(1))] == ["sku-grinder", "sku-mug"]
    assert [s.id for s in shop.catalog.search(HasAttribute("colour", "black"))] == ["sku-kettle"]


def test_stale_version_is_rejected_by_the_row(clock: FakeClock) -> None:
    inventory = InventoryService([EAST], clock=clock, ids=SequentialIdGenerator("H"))
    row = inventory.add_stock("sku-a", EAST.id, 10)
    stale = row.version
    inventory.add_stock("sku-a", EAST.id, 5)  # somebody else wrote the row
    with pytest.raises(ConflictError):
        row.hold(1, expected_version=stale)
    row.hold(1, expected_version=row.version)  # the fresh version still works
    assert (row.available, row.reserved) == (14, 1)


def test_the_bus_isolates_failures_and_still_drives_shipments_and_inboxes(clock: FakeClock) -> None:
    shop = build(clock, stock={("sku-kettle", EAST.id): 3})
    notifications = NotificationService(shop.bus)
    alerts = LowStockMonitor(shop.bus, shop.inventory, threshold=2)
    shipments = ShipmentDispatcher(shop.bus, shop.orders, SequentialIdGenerator("SHP"))
    shop.bus.subscribe(ORDER_PLACED, lambda e: (_ for _ in ()).throw(RuntimeError("email down")))

    order = shop.checkout(cart_with(shop, "cust-1", kettle=2), "cust-1", SHIP_TO, "k1")
    shop.pay(order.id)
    assert shop.bus.failures() == [(ORDER_PLACED, "email down")]  # recorded, not raised
    assert alerts.alerts() == [("sku-kettle", 1)]
    assert shipments.shipment_for(order.id).tracking == f"TRK-{order.id}"
    assert order.shipment_id is not None
    assert notifications.inbox("cust-1") == [
        f"order {order.id} received, total {order.total}",
        f"payment taken for {order.id}",
    ]


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (NoDiscount(), "0.00"),
        (PercentOff(Decimal("0.10"), Money.of("15.00")), "8.75"),
        (PercentOff(Decimal("0.50"), Money.of("15.00")), "15.00"),  # the cap bites
        (PercentOff(Decimal("0.10"), Money.of("15.00"), Money.of("100.00")), "0.00"),  # below the floor
        (CheapestFreeInBundle(3), "9.50"),  # 3 units, the mug is free
    ],
)
def test_discount_strategies(clock: FakeClock, strategy: DiscountStrategy, expected: str) -> None:
    shop = build(clock, discount=strategy)
    order = shop.checkout(cart_with(shop, "cust-1", kettle=2, mug=1), "cust-1", SHIP_TO, "k1")
    assert order.discount == Money.of(expected)
