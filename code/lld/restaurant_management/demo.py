"""A Friday service: seat, order, send, void, serve, split the bill, clear the table."""

from decimal import Decimal

from common import FakeClock, Money, SequentialIdGenerator
from lld.restaurant_management.commands import ChangeQuantity
from lld.restaurant_management.menu import ComboItem, MenuItem, MenuSection
from lld.restaurant_management.models import (
    Modifier,
    OrderStateError,
    PaymentMethod,
    Shift,
    Staff,
    StaffRole,
    Table,
    TableUnavailableError,
)
from lld.restaurant_management.pos import PointOfSale, Restaurant
from lld.restaurant_management.services import FloorPlan, Kitchen, KitchenDisplay, WaiterPager
from lld.restaurant_management.strategies import EvenSplit, LargePartyDiscount

SERVICE_START = 1_773_248_400.0  # 2026-03-11 17:00 UTC


def build_menu() -> MenuSection:
    soup = MenuItem("m-soup", "Tomato soup", Money.of("6.00"))
    salad = MenuItem("m-salad", "Garden salad", Money.of("7.50"))
    steak = MenuItem("m-steak", "Ribeye", Money.of("28.00"), modifiers=(Modifier("medium rare"),))
    risotto = MenuItem("m-risotto", "Mushroom risotto", Money.of("19.00"), available=False)
    cake = MenuItem("m-cake", "Chocolate cake", Money.of("8.00"))
    starters = MenuSection("s-starters", "Starters", [soup, salad])
    mains = MenuSection("s-mains", "Mains", [steak, risotto])
    desserts = MenuSection("s-desserts", "Desserts", [cake])
    combo = ComboItem("c-prix-fixe", "Prix fixe", [soup, steak, cake], discount=Decimal("0.15"))
    return MenuSection("s-menu", "Menu", [starters, mains, desserts, combo])


def main() -> None:
    clock = FakeClock(start=SERVICE_START)
    menu = build_menu()
    floor = FloorPlan([Table("T1", 2), Table("T2", 4), Table("T3", 4), Table("T4", 6)])
    kitchen = Kitchen(clock=clock, ids=SequentialIdGenerator("KT"))
    display, pager = KitchenDisplay(), WaiterPager()
    kitchen.subscribe(display)
    kitchen.subscribe(pager)
    restaurant = Restaurant("The Copper Pot", floor, menu, tax_rate=Decimal("0.08"))
    pos = PointOfSale(
        restaurant,
        kitchen,
        discount=LargePartyDiscount(Money.of("60.00"), Money.of("5.00")),
        clock=clock,
        ids=SequentialIdGenerator("ID"),
    )
    manager = Staff("st-1", "Nadia", StaffRole.MANAGER)

    print(f"prix fixe {menu.require('c-prix-fixe').price()} vs a la carte {Money.of('42.00')} (15% off)")
    print(f"risotto orderable: {menu.require('m-risotto').is_available()}; mains section still on: {menu.require('s-mains').is_available()}")

    booking = pos.book("Rao", 4, SERVICE_START + 7200, ["T3"])
    print(f"{booking.id} holds T3, table is now {floor.table('T3').status}")
    waiting = pos.join_waitlist("Iyer", 2)
    print(f"waitlist: {waiting.guest_name} party of {waiting.party_size}, quoted {waiting.quoted_wait_minutes} min")
    entry, order = pos.seat_next_walk_in(server_id="st-2")
    print(f"seated {entry.guest_name} at {','.join(order.table_ids)} -> order {order.id}")

    pos.add_item(order.id, "c-prix-fixe", quantity=2)
    fries = pos.add_item(order.id, "m-salad", quantity=3)
    pos.apply_edit(order.id, ChangeQuantity(fries.id, 1))
    print(f"undo: {pos.undo_last_edit(order.id)}; tab is now {order.subtotal()}")

    ticket = pos.send_to_kitchen(order.id)
    print(f"{ticket.id} -> kitchen: {list(ticket.lines)}")
    try:
        pos.apply_edit(order.id, ChangeQuantity(fries.id, 5))
    except OrderStateError as exc:
        print(f"edit after send rejected: {exc}")
    pos.void_line(order.id, fries.id, "guest changed their mind", manager)
    kitchen.advance(ticket.id)
    kitchen.advance(ticket.id)
    print(f"pager: {pager.pages()}; board: {display.render()}")

    kitchen.advance(ticket.id)
    pos.mark_served(order.id)
    bill = pos.bill(order.id, split=EvenSplit(3))
    print(f"bill {bill.subtotal} - {bill.discount} discount + {bill.tax} tax = {bill.total}")
    print(f"split three ways: {[str(s) for s in bill.shares]} (adds up: {bill.shares_add_up()})")
    pos.pay(bill.id, PaymentMethod.CARD)
    cleared = pos.clear_table(order.id)
    print(f"tables {','.join(cleared)} -> {floor.table(cleared[0]).status}")
    try:
        pos.seat_party(8, "st-2", ["T1", "T2"])
    except TableUnavailableError as exc:
        print(f"joining tables rejected: {exc}")
    print(f"party of 8 joins T2+T4 -> order {pos.seat_party(8, 'st-2', ['T4', 'T2']).id}")
    report = pos.daily_report(Shift("dinner", SERVICE_START, SERVICE_START + 21600))
    print(f"{report['shift']} report: {report['tabs']} tabs, {report['revenue']} taken, {report['tables_used']} tables used")


if __name__ == "__main__":
    main()
