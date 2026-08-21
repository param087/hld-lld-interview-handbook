"""Table assignment races, edits after the ticket is sent, and split-bill rounding."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from common import FakeClock, Money, SequentialIdGenerator, ValidationError
from lld.restaurant_management.commands import AddModifier, ChangeQuantity
from lld.restaurant_management.menu import ComboItem, MenuItem, MenuSection
from lld.restaurant_management.models import (
    ItemUnavailableError,
    Modifier,
    Order,
    OrderItem,
    OrderStateError,
    PaymentMethod,
    Shift,
    Staff,
    StaffRole,
    Table,
    TableStatus,
    TableUnavailableError,
    TicketStatus,
)
from lld.restaurant_management.pos import PointOfSale, Restaurant
from lld.restaurant_management.services import FloorPlan, Kitchen, KitchenDisplay, WaiterPager
from lld.restaurant_management.strategies import (
    ByItemSplit,
    EvenSplit,
    LargePartyDiscount,
    PercentageDiscount,
)

SERVICE_START = 1_773_248_400.0  # 2026-03-11 17:00 UTC
MANAGER = Staff("st-1", "Nadia", StaffRole.MANAGER)
SERVER = Staff("st-2", "Ivan", StaffRole.SERVER)


def build_menu() -> MenuSection:
    soup = MenuItem("m-soup", "Tomato soup", Money.of("6.00"))
    salad = MenuItem("m-salad", "Garden salad", Money.of("7.50"))
    steak = MenuItem("m-steak", "Ribeye", Money.of("28.00"))
    risotto = MenuItem("m-risotto", "Mushroom risotto", Money.of("19.00"), available=False)
    cake = MenuItem("m-cake", "Chocolate cake", Money.of("8.00"))
    combo = ComboItem("c-prix-fixe", "Prix fixe", [soup, steak, cake], discount=Decimal("0.15"))
    return MenuSection(
        "s-menu",
        "Menu",
        [
            MenuSection("s-starters", "Starters", [soup, salad]),
            MenuSection("s-mains", "Mains", [steak, risotto]),
            MenuSection("s-desserts", "Desserts", [cake]),
            combo,
        ],
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=SERVICE_START)


def build_pos(clock: FakeClock, **kwargs: object) -> tuple[PointOfSale, FloorPlan, Kitchen]:
    floor = FloorPlan([Table("T1", 2), Table("T2", 4), Table("T3", 4), Table("T4", 6)])
    kitchen = Kitchen(clock=clock, ids=SequentialIdGenerator("KT"))
    restaurant = Restaurant("The Copper Pot", floor, build_menu(), tax_rate=Decimal("0.08"))
    pos = PointOfSale(restaurant, kitchen, clock=clock, ids=SequentialIdGenerator("ID"), **kwargs)
    return pos, floor, kitchen


def test_seat_order_send_serve_bill_and_clear(clock: FakeClock) -> None:
    pos, floor, kitchen = build_pos(clock)
    display, pager = KitchenDisplay(), WaiterPager()
    kitchen.subscribe(display)
    kitchen.subscribe(pager)

    order = pos.seat_party(2, SERVER.id, ["T1"])
    assert floor.table("T1").status is TableStatus.OCCUPIED
    pos.add_item(order.id, "m-steak", quantity=2)
    assert order.subtotal() == Money.of("56.00")

    ticket = pos.send_to_kitchen(order.id)
    assert ticket.lines == ("2 x Ribeye",) and ticket.status is TicketStatus.QUEUED
    kitchen.advance(ticket.id)  # PREPARING
    kitchen.advance(ticket.id)  # READY
    assert pager.pages() == ["pickup for T1 (KT-1)"]
    assert "KT-1 T1 ready" in display.render()

    kitchen.advance(ticket.id)  # SERVED
    pos.mark_served(order.id)
    bill = pos.bill(order.id)
    assert bill.subtotal == Money.of("56.00")
    assert bill.tax == Money.of("4.48") and bill.total == Money.of("60.48")
    pos.pay(bill.id, PaymentMethod.CARD)
    assert pos.clear_table(order.id) == ("T1",)
    assert floor.table("T1").status is TableStatus.CLEANING
    floor.mark_clean("T1")
    assert floor.table("T1").status is TableStatus.AVAILABLE


# --8<-- [start:edit_after_send]
def test_edits_are_free_until_the_ticket_is_sent_then_need_a_manager(clock: FakeClock) -> None:
    pos, _, kitchen = build_pos(clock)
    order = pos.seat_party(2, SERVER.id, ["T1"])
    soup = pos.add_item(order.id, "m-soup", quantity=2)
    pos.apply_edit(order.id, ChangeQuantity(soup.id, 4))
    assert order.subtotal() == Money.of("24.00")
    assert pos.undo_last_edit(order.id) == f"set line {soup.id} to 4"
    assert order.subtotal() == Money.of("12.00")  # back to two soups

    ticket = pos.send_to_kitchen(order.id)
    with pytest.raises(OrderStateError):
        pos.apply_edit(order.id, ChangeQuantity(soup.id, 1))  # the docket is on the pass
    with pytest.raises(OrderStateError, match="cannot void"):
        pos.void_line(order.id, soup.id, "changed their mind", SERVER)
    pos.void_line(order.id, soup.id, "changed their mind", MANAGER)
    assert order.subtotal() == Money.of("0.00")
    assert kitchen.board()[0].lines[-1] == "VOID 2 x Tomato soup"
    assert ticket.status is TicketStatus.QUEUED  # the void does not advance the ticket


# --8<-- [end:edit_after_send]


def test_menu_composite_prices_a_combo_and_hides_an_unavailable_dish(clock: FakeClock) -> None:
    pos, _, _ = build_pos(clock)
    menu = build_menu()
    assert menu.require("c-prix-fixe").price() == Money.of("35.70")  # 42.00 less 15%
    assert menu.require("s-mains").is_available() is True  # the steak is still on
    assert menu.require("m-risotto").is_available() is False
    assert len(menu.leaves()) == 8  # five dishes, three of them repeated inside the combo

    order = pos.seat_party(2, SERVER.id, ["T1"])
    with pytest.raises(ItemUnavailableError, match="Mushroom risotto"):
        pos.add_item(order.id, "m-risotto")


def test_combo_is_unavailable_when_any_component_is() -> None:
    soup = MenuItem("m-soup", "Soup", Money.of("6.00"))
    steak = MenuItem("m-steak", "Steak", Money.of("28.00"), available=False)
    section = MenuSection("s", "Section", [soup, steak])
    combo = ComboItem("c", "Combo", [soup, steak], discount=Decimal("0.10"))
    assert section.is_available() is True  # any child
    assert combo.is_available() is False  # every child


@pytest.mark.parametrize(
    ("total", "ways", "expected"),
    [
        ("60.00", 3, ["20.00", "20.00", "20.00"]),
        ("10.00", 3, ["3.34", "3.33", "3.33"]),  # the odd cent goes to the first guest
        ("0.05", 4, ["0.02", "0.01", "0.01", "0.01"]),
    ],
)
def test_even_split_never_loses_a_cent(total: str, ways: int, expected: list[str]) -> None:
    order = Order(id="ID-1", table_ids=("T1",), server_id="s", opened_at=0.0)
    shares = EvenSplit(ways).split(order, Money.of(total))
    assert [str(s).removesuffix(" USD") for s in shares] == expected
    summed = Money(0)
    for share in shares:
        summed = summed + share
    assert summed == Money.of(total)


def test_by_item_split_shares_tax_and_discount_pro_rata(clock: FakeClock) -> None:
    pos, _, _ = build_pos(clock, discount=PercentageDiscount(Decimal("10")))
    order = pos.seat_party(4, SERVER.id, ["T2"])
    steak = pos.add_item(order.id, "m-steak")  # 28.00
    soup = pos.add_item(order.id, "m-soup")  # 6.00
    pos.send_to_kitchen(order.id)
    pos.mark_served(order.id)
    bill = pos.bill(order.id, split=ByItemSplit(((steak.id,), (soup.id,))))
    assert bill.subtotal == Money.of("34.00") and bill.discount == Money.of("3.40")
    assert bill.total == Money.of("33.05")  # 30.60 plus 8% tax
    assert [str(s) for s in bill.shares] == ["27.22 USD", "5.83 USD"]
    assert bill.shares_add_up()


def test_modifiers_change_the_line_total_and_the_docket(clock: FakeClock) -> None:
    pos, _, kitchen = build_pos(clock)
    order = pos.seat_party(2, SERVER.id, ["T1"])
    steak = pos.add_item(order.id, "m-steak", modifiers=(Modifier("medium rare"),))
    pos.apply_edit(order.id, AddModifier(steak.id, Modifier("extra sauce", Money.of("2.00"))))
    assert order.subtotal() == Money.of("30.00")
    ticket = pos.send_to_kitchen(order.id)
    assert ticket.lines == ("1 x Ribeye (medium rare, extra sauce)",)


def test_reservation_blocks_a_walk_in_but_not_the_holder(clock: FakeClock) -> None:
    pos, floor, _ = build_pos(clock)
    booking = pos.book("Rao", 4, SERVICE_START + 7200, ["T3"])
    assert floor.table("T3").status is TableStatus.RESERVED
    with pytest.raises(TableUnavailableError):
        pos.seat_party(4, SERVER.id, ["T3"])  # a walk-in cannot take a held table
    order = pos.seat_party(4, SERVER.id, ["T3"], reservation_id=booking.id)
    assert floor.table("T3").order_id == order.id


def test_waitlist_is_fifo_and_skips_a_party_that_still_does_not_fit(clock: FakeClock) -> None:
    pos, floor, _ = build_pos(clock)
    pos.seat_party(6, SERVER.id, ["T4"])
    pos.seat_party(4, SERVER.id, ["T2"])
    pos.seat_party(4, SERVER.id, ["T3"])  # only T1 (two seats) is left
    big = pos.join_waitlist("Rao", 6)
    small = pos.join_waitlist("Iyer", 2)
    seated = pos.seat_next_walk_in(SERVER.id)
    assert seated is not None
    assert seated[0].id == small.id  # the party of six waits, the two-top is seated
    assert [e.id for e in pos.waitlist()] == [big.id]
    assert floor.table("T1").status is TableStatus.OCCUPIED


def test_invalid_requests_are_rejected(clock: FakeClock) -> None:
    pos, _, _ = build_pos(clock)
    with pytest.raises(TableUnavailableError, match="does not fit"):
        pos.seat_party(6, SERVER.id, ["T1"])  # two seats
    with pytest.raises(ValidationError):
        pos.seat_party(0, SERVER.id, ["T2"])
    with pytest.raises(ValidationError):
        EvenSplit(0)
    with pytest.raises(ValidationError):
        PercentageDiscount(Decimal("120"))
    order = pos.seat_party(2, SERVER.id, ["T1"])
    with pytest.raises(OrderStateError, match="nothing to send"):
        pos.send_to_kitchen(order.id)


def test_discount_threshold_and_daily_report(clock: FakeClock) -> None:
    pos, _, _ = build_pos(clock, discount=LargePartyDiscount(Money.of("60.00"), Money.of("5.00")))
    order = pos.seat_party(4, SERVER.id, ["T2"])
    pos.add_item(order.id, "m-steak", quantity=3)  # 84.00, over the threshold
    pos.send_to_kitchen(order.id)
    pos.mark_served(order.id)
    bill = pos.bill(order.id)
    assert bill.discount == Money.of("5.00")
    pos.pay(bill.id, PaymentMethod.CASH)
    report = pos.daily_report(Shift("dinner", SERVICE_START, SERVICE_START + 21_600))
    assert report["tabs"] == 1 and report["revenue"] == bill.total


def test_order_item_line_total_with_modifiers() -> None:
    item = OrderItem(
        id="ID-1",
        component_id="m-steak",
        name="Ribeye",
        unit_price=Money.of("28.00"),
        quantity=2,
        modifiers=(Modifier("extra sauce", Money.of("2.00")), Modifier("no salt")),
    )
    assert item.line_total() == Money.of("60.00")  # (28.00 + 2.00) x 2


# --8<-- [start:concurrency]
def test_twenty_hosts_race_for_four_tables_and_never_double_seat(clock: FakeClock) -> None:
    pos, floor, _ = build_pos(clock)
    plans = [["T1"], ["T2", "T4"], ["T3"], ["T4"], ["T2"], ["T2", "T3"]]

    def seat(i: int) -> tuple[str, ...] | None:
        try:
            return pos.seat_party(2, SERVER.id, plans[i % len(plans)]).table_ids
        except TableUnavailableError:
            return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(seat, range(20)))

    claimed = [t for tables in results if tables is not None for t in tables]
    assert len(claimed) == len(set(claimed))  # no table seated by two parties
    occupied = [t.id for t in floor.tables() if t.status is TableStatus.OCCUPIED]
    assert sorted(claimed) == occupied  # every claim is on the floor, nothing else is


# --8<-- [end:concurrency]
