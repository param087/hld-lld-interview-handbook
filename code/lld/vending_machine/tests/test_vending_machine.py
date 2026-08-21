from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, Money, SequentialIdGenerator, ValidationError
from lld.vending_machine.beverages import BasicBeverage, BeverageFactory, ExtraShot, Sweetened
from lld.vending_machine.models import (
    CashBox,
    Coin,
    DispenseFailedError,
    Dispenser,
    IllegalActionError,
    Ingredient,
    InsufficientChangeError,
    InsufficientFundsError,
    OutOfIngredientError,
    OutOfStockError,
    Product,
    Recipe,
    Slot,
    UnknownSlotError,
)
from lld.vending_machine.services import (
    CoffeeBar,
    IngredientInventory,
    Inventory,
    MaintenanceLog,
    VendingMachine,
)
from lld.vending_machine.strategies import ChangeMaker, GreedyChangeMaker, MinimalChangeMaker

ESPRESSO = Recipe("espresso", Money.of("1.20"), {Ingredient.WATER: 30, Ingredient.BEANS: 7})
LATTE = Recipe(
    "latte", Money.of("1.80"), {Ingredient.WATER: 30, Ingredient.BEANS: 7, Ingredient.MILK: 120}
)


class JammedDispenser:
    """The motor takes the item off the shelf and then fails to drop it."""

    def eject(self, item: str) -> bool:
        return False


def build(
    coins: Mapping[Coin, int] | None = None,
    dispenser: Dispenser | None = None,
) -> tuple[VendingMachine, Inventory]:
    slots = [
        Slot("A1", Product("A1", "cola", Money.of("1.50")), quantity=2),
        Slot("A2", Product("A2", "chips", Money.of("1.00")), quantity=1),
        Slot("B1", Product("B1", "juice", Money.of("1.60")), quantity=3),
    ]
    inventory = Inventory(slots, low_stock_at=1)
    machine = VendingMachine(
        inventory,
        cash=CashBox(coins=dict(coins or {})),
        dispenser=dispenser,
        clock=FakeClock(1000.0),
        ids=SequentialIdGenerator("TX"),
    )
    return machine, inventory


def fund(machine: VendingMachine) -> None:
    machine.insert(Coin.DOLLAR)
    machine.insert(Coin.HALF)


def reserve_cola(machine: VendingMachine) -> None:
    fund(machine)
    machine.select("A1")


def offline(machine: VendingMachine) -> None:
    machine.take_offline()


def test_buying_a_snack_charges_the_price_and_returns_the_change() -> None:
    machine, inventory = build(coins={Coin.QUARTER: 2, Coin.TEN: 2})
    machine.insert(Coin.DOLLAR)
    machine.insert(Coin.DOLLAR)
    machine.select("A1")
    assert machine.status() == "Dispensing"
    sale = machine.dispense()
    assert (sale.id, sale.item, sale.price) == ("TX-1", "cola", Money.of("1.50"))
    assert sale.change == (Coin.QUARTER, Coin.QUARTER) and sale.at == 1000.0
    assert machine.status() == "Idle" and machine.balance() == Money(0)
    assert inventory.stock("A1") == 1
    assert machine.cash_total() == Money.of("2.20")  # 0.70 float + 2.00 in - 0.50 out


@pytest.mark.parametrize(
    ("setup", "action", "message"),
    [
        (lambda m: None, lambda m: m.dispense(), "cannot dispense while Idle"),
        (lambda m: None, lambda m: m.select("A1"), "cannot select while Idle"),
        (lambda m: None, lambda m: m.cancel(), "nothing to cancel while Idle"),
        (fund, lambda m: m.dispense(), "cannot dispense while HasMoney"),
        (reserve_cola, lambda m: m.insert(Coin.DOLLAR), "cannot insert money while Dispensing"),
        (reserve_cola, lambda m: m.select("A2"), "cannot select while Dispensing"),
        (offline, lambda m: m.insert(Coin.DOLLAR), "cannot insert money while OutOfService"),
    ],
)
def test_an_illegal_event_is_refused_and_names_the_state(
    setup: Callable[[VendingMachine], None],
    action: Callable[[VendingMachine], object],
    message: str,
) -> None:
    machine, _ = build(coins={Coin.QUARTER: 4})
    setup(machine)
    with pytest.raises(IllegalActionError) as exc:
        action(machine)
    assert message in str(exc.value)


# --8<-- [start:validation]
@pytest.mark.parametrize(
    ("code", "error", "message"),
    [
        ("A1", InsufficientFundsError, "insert 0.50 USD more"),
        ("ZZ", UnknownSlotError, "no slot 'ZZ'"),
    ],
)
def test_a_rejected_selection_leaves_the_balance_and_the_stock_alone(
    code: str, error: type[Exception], message: str
) -> None:
    machine, inventory = build(coins={Coin.QUARTER: 4})
    machine.insert(Coin.DOLLAR)
    with pytest.raises(error) as exc:
        machine.select(code)
    assert message in str(exc.value)
    assert machine.status() == "HasMoney"  # validate first, transition last
    assert machine.balance() == Money.of("1.00") and inventory.stock("A1") == 2


# --8<-- [end:validation]


def test_an_empty_slot_is_refused_after_its_last_item() -> None:
    machine, inventory = build(coins={Coin.QUARTER: 4})
    machine.insert(Coin.DOLLAR)
    machine.select("A2")
    machine.dispense()
    assert inventory.stock("A2") == 0
    machine.insert(Coin.DOLLAR)
    with pytest.raises(OutOfStockError):
        machine.select("A2")
    assert machine.status() == "HasMoney"


def test_a_purchase_that_cannot_be_changed_is_refused_before_anything_moves() -> None:
    machine, inventory = build(coins={Coin.QUARTER: 1, Coin.TEN: 1})  # 0.35 in the box
    machine.insert(Coin.DOLLAR)
    machine.insert(Coin.DOLLAR)
    with pytest.raises(InsufficientChangeError):
        machine.select("B1")  # 1.60, so 0.40 change, and 0.40 cannot be made
    assert inventory.stock("B1") == 3 and machine.balance() == Money.of("2.00")
    assert machine.cancel() == Money.of("2.00") and machine.status() == "Idle"


@pytest.mark.parametrize("maker", [GreedyChangeMaker(), MinimalChangeMaker()])
def test_both_change_makers_agree_when_greedy_works(maker: ChangeMaker) -> None:
    box = {Coin.QUARTER: 1, Coin.TEN: 2, Coin.FIVE: 1}
    assert maker.plan(Money(40), box) == (Coin.QUARTER, Coin.TEN, Coin.FIVE)


def test_greedy_change_gives_up_where_a_combination_exists() -> None:
    box = {Coin.QUARTER: 1, Coin.TEN: 3}
    with pytest.raises(InsufficientChangeError):
        GreedyChangeMaker().plan(Money(30), box)  # takes the quarter, then needs a nickel
    assert MinimalChangeMaker().plan(Money(30), box) == (Coin.TEN, Coin.TEN, Coin.TEN)


# --8<-- [start:jam]
def test_a_jam_refunds_the_customer_and_puts_the_item_back() -> None:
    machine, inventory = build(dispenser=JammedDispenser())
    fund(machine)
    machine.select("A1")
    assert inventory.stock("A1") == 1  # held for this customer
    with pytest.raises(DispenseFailedError):
        machine.dispense()
    assert inventory.stock("A1") == 2  # back on the shelf
    assert machine.balance() == Money(0) and machine.cash_total() == Money(0)  # coins returned
    assert machine.status() == "OutOfService"
    machine.bring_online()
    assert machine.status() == "Idle"


# --8<-- [end:jam]


# --8<-- [start:concurrency]
def test_thirty_two_threads_selecting_produce_exactly_one_dispense() -> None:
    machine, inventory = build(coins={Coin.QUARTER: 4})
    machine.insert(Coin.DOLLAR)
    machine.insert(Coin.HALF)

    def select(_: int) -> bool:
        try:
            machine.select("A1")
            return True
        except IllegalActionError:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(select, range(32)))

    assert results.count(True) == 1  # one winner, thirty-one refusals
    assert inventory.stock("A1") == 1 and machine.status() == "Dispensing"
    assert machine.dispense().item == "cola"


# --8<-- [end:concurrency]


def test_restocking_is_atomic_under_concurrency() -> None:
    slot = Slot("A1", Product("A1", "cola", Money.of("1.50")), quantity=0, capacity=100)
    inventory = Inventory([slot])

    def restock(_: int) -> None:
        inventory.restock("A1", 1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(restock, range(64)))
    assert inventory.stock("A1") == 64


def test_going_offline_refunds_the_customer_first() -> None:
    machine, _ = build(coins={Coin.QUARTER: 4})
    machine.insert(Coin.DOLLAR)
    assert machine.take_offline() == Money.of("1.00")
    assert machine.status() == "OutOfService"
    assert machine.collect_cash() == Money.of("1.00")  # the float, the refund already paid out
    machine.bring_online()
    assert machine.status() == "Idle"


def test_the_operator_cannot_empty_the_box_under_a_customer() -> None:
    machine, _ = build(coins={Coin.QUARTER: 4})
    machine.insert(Coin.DOLLAR)
    with pytest.raises(IllegalActionError):
        machine.collect_cash()
    machine.cancel()
    assert machine.collect_cash(leave={Coin.QUARTER: 1}) == Money.of("0.75")


def test_low_stock_reaches_the_maintenance_log_without_polling() -> None:
    machine, inventory = build(coins={Coin.QUARTER: 4})
    log = MaintenanceLog()
    inventory.subscribe(log)
    machine.insert(Coin.DOLLAR)
    machine.select("A2")
    machine.dispense()
    assert log.lines() == ["low stock: chips (A2) down to 0"]


def test_the_factory_builds_the_same_stack_from_configuration() -> None:
    drink = BeverageFactory.create(LATTE, "shot", "sugar")
    assert drink.name() == "latte + extra shot + sugar"
    assert drink.price() == Sweetened(ExtraShot(BasicBeverage(LATTE))).price()
    with pytest.raises(ValidationError):
        BeverageFactory.create(LATTE, "caramel")


def test_decorators_add_price_and_ingredients_without_a_subclass() -> None:
    drink = Sweetened(ExtraShot(BasicBeverage(LATTE)))
    assert drink.name() == "latte + extra shot + sugar"
    assert drink.price() == Money.of("2.30")  # 1.80 + 0.50 + 0.00
    assert drink.ingredients()[Ingredient.BEANS] == 14  # 7 + 7
    assert drink.ingredients()[Ingredient.SUGAR] == 6


def test_the_coffee_machine_is_the_same_machine_over_a_different_source() -> None:
    pantry = IngredientInventory(
        {Ingredient.WATER: 100, Ingredient.BEANS: 8, Ingredient.MILK: 10}, low_level_at=5
    )
    bar = CoffeeBar({"C1": BasicBeverage(ESPRESSO), "C2": BasicBeverage(LATTE)}, pantry)
    machine = VendingMachine(
        bar, cash=CashBox(coins={Coin.QUARTER: 3, Coin.TEN: 2, Coin.FIVE: 1}), clock=FakeClock(0.0)
    )
    machine.insert(Coin.DOLLAR)
    machine.insert(Coin.DOLLAR)
    with pytest.raises(OutOfIngredientError):
        machine.select("C2")  # a latte needs 120 ml of milk and 10 are left
    assert pantry.level(Ingredient.BEANS) == 8  # all-or-nothing: nothing was taken
    machine.select("C1")
    cup = machine.dispense()
    assert cup.item == "espresso" and cup.change_amount() == Money.of("0.80")
    assert pantry.level(Ingredient.BEANS) == 1
