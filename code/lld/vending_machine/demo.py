"""One snack machine and one coffee machine: the same class over two item sources."""

from common import FakeClock, Money, SequentialIdGenerator
from lld.vending_machine.beverages import BeverageFactory
from lld.vending_machine.models import (
    CashBox,
    Coin,
    IllegalActionError,
    Ingredient,
    InsufficientChangeError,
    Note,
    OutOfIngredientError,
    Product,
    Recipe,
    Slot,
)
from lld.vending_machine.services import (
    CoffeeBar,
    IngredientInventory,
    Inventory,
    MaintenanceLog,
    VendingMachine,
)

SNACKS = [
    Slot("A1", Product("A1", "cola", Money.of("1.50")), quantity=2),
    Slot("A2", Product("A2", "chips", Money.of("1.00")), quantity=4),
    Slot("B1", Product("B1", "juice", Money.of("1.60")), quantity=3),
]
ESPRESSO = Recipe("espresso", Money.of("1.20"), {Ingredient.WATER: 30, Ingredient.BEANS: 7})
LATTE = Recipe(
    "latte", Money.of("1.80"), {Ingredient.WATER: 30, Ingredient.BEANS: 7, Ingredient.MILK: 120}
)


def snack_machine(log: MaintenanceLog) -> tuple[VendingMachine, Inventory]:
    inventory = Inventory(SNACKS, low_stock_at=1)
    inventory.subscribe(log)
    cash = CashBox(coins={Coin.QUARTER: 2, Coin.TEN: 3, Coin.FIVE: 3})
    machine = VendingMachine(
        inventory, cash=cash, clock=FakeClock(1_700_000_000), ids=SequentialIdGenerator("TX")
    )
    return machine, inventory


def coffee_machine(log: MaintenanceLog) -> VendingMachine:
    pantry = IngredientInventory(
        {Ingredient.WATER: 500, Ingredient.BEANS: 20, Ingredient.MILK: 200, Ingredient.SUGAR: 50},
        low_level_at=40,
    )
    pantry.subscribe(log)
    menu = {
        "C1": BeverageFactory.create(ESPRESSO),
        "C2": BeverageFactory.create(LATTE, "shot", "milk", "sugar"),
    }
    cash = CashBox(coins={Coin.QUARTER: 2, Coin.TEN: 4, Coin.FIVE: 4})
    return VendingMachine(
        CoffeeBar(menu, pantry), cash=cash, clock=FakeClock(1_700_000_100),
        ids=SequentialIdGenerator("CX"),
    )


def main() -> None:
    log = MaintenanceLog()
    machine, inventory = snack_machine(log)
    print("--- snack machine: A1 cola 1.50, A2 chips 1.00, B1 juice 1.60 ---")
    for coin in (Coin.DOLLAR, Coin.HALF):
        machine.insert(coin)
        print(f"insert {coin.money}         -> {machine.status()}, balance {machine.balance()}")
    machine.select("A1")
    print(f"select A1               -> {machine.status()}, cola held for this customer")
    sale = machine.dispense()
    print(f"dispense                -> {sale.item}, change {sale.change_amount()}, {machine.status()}")
    try:
        machine.dispense()
    except IllegalActionError as exc:
        print(f"dispense again          refused: {exc}")

    collected = machine.collect_cash(leave={Coin.QUARTER: 1, Coin.TEN: 1})
    print(f"operator collects {collected}, leaving {machine.cash_total()} for change")
    machine.insert(Coin.DOLLAR)
    machine.insert(Coin.DOLLAR)
    try:
        machine.select("B1")
    except InsufficientChangeError as exc:
        print(f"select B1 with 2.00 USD refused: {exc}")
    print(f"cancel                  -> refunded {machine.cancel()}, {machine.status()}")
    trail = [machine.transitions[0][0], *(after for _, after in machine.transitions)]
    print("transitions: " + " -> ".join(trail))

    coffee = coffee_machine(log)
    print("--- the same machine over recipes: C1 espresso, C2 latte with add-ons ---")
    for _ in range(3):
        coffee.insert(Coin.DOLLAR)
    coffee.insert(Note.ONE)
    coffee.select("C2")
    cup = coffee.dispense()
    print(f"select C2 (4.00 USD in) -> {cup.item} at {cup.price}, change {cup.change_amount()}")
    try:
        coffee.insert(Coin.DOLLAR)
        coffee.insert(Coin.DOLLAR)
        coffee.insert(Coin.HALF)
        coffee.insert(Coin.FIVE)
        coffee.select("C2")
    except OutOfIngredientError as exc:
        print(f"select C2 again         refused: {exc}")
    print(f"cancel                  -> refunded {coffee.cancel()}")

    inventory.restock("A1", 4)
    print(f"restock A1 by 4         -> {inventory.stock('A1')} in the slot")
    for line in log.lines():
        print(line)


if __name__ == "__main__":
    main()
