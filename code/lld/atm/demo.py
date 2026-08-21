"""One customer, two machines: a wrong PIN, a withdrawal, two refusals, and a jam."""

from common import FakeClock, HandbookError, Money, SequentialIdGenerator
from lld.atm.bank import InMemoryBank
from lld.atm.dispenser import CashDispenser, JammingFeeder
from lld.atm.models import Card
from lld.atm.services import ATM

NOTES = [Money.of("100.00"), Money.of("50.00"), Money.of("20.00"), Money.of("10.00")]


def build_bank(clock: FakeClock) -> tuple[InMemoryBank, Card]:
    bank = InMemoryBank(clock=clock, ids=SequentialIdGenerator("TXN"))
    bank.open_account("ACC-1", "Ada", Money.of("1200.00"))
    bank.open_account("ACC-2", "Ada", Money.of("500.00"))
    card = bank.issue_card("4111-2222", "Ada", pin="4321", account_ids=("ACC-1", "ACC-2"))
    return bank, card


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    bank, card = build_bank(clock)
    counts = dict(zip(NOTES, [20, 10, 10, 10], strict=True))
    atm = ATM("atm-1", bank, CashDispenser(counts), clock=clock)
    print(f"{atm.id} is {atm.state} with {atm.dispenser.total()} in the cassettes")

    atm.insert_card(card)
    try:
        atm.enter_pin("1111")
    except HandbookError as exc:
        print(f"{atm.id}: {exc}")
    atm.enter_pin("4321")
    print(f"{atm.id} is {atm.state}: {atm.screen.last()}")
    print(f"balance of ACC-1: {atm.check_balance()}")

    receipt = atm.withdraw(Money.of("180.00"))
    print(f"withdrew 180 -> {receipt.note_summary()}, balance {receipt.record.balance_after}")
    for amount in ("25.00", "400.00"):
        try:
            atm.withdraw(Money.of(amount))
        except HandbookError as exc:
            print(f"{amount} refused: {exc}")

    print(f"deposit 300 -> balance {atm.deposit(Money.of('300.00')).record.balance_after}")
    transfer = atm.transfer("ACC-2", Money.of("200.00"))
    print(f"transfer 200 -> ACC-1 {transfer.record.balance_after}, ACC-2 {bank.balance('ACC-2')}")
    print(f"mini statement: {len(atm.mini_statement())} entries, last {atm.mini_statement()[-1].type}")
    atm.cancel()
    print(f"{atm.id} is {atm.state} again: {atm.screen.last()}")

    broken = ATM("atm-2", bank, CashDispenser(counts, feeder=JammingFeeder()), clock=clock)
    broken.insert_card(card)
    broken.enter_pin("4321")
    before = broken.check_balance()
    try:
        broken.withdraw(Money.of("100.00"))
    except HandbookError as exc:
        print(f"{broken.id}: {exc}")
    print(f"{broken.id} is {broken.state}; ACC-1 still holds {bank.balance('ACC-1')} (was {before})")
    refilled = broken.replenish({NOTES[0]: 10})
    print(f"admin replenish -> {broken.id} is {broken.state} with {refilled}")


if __name__ == "__main__":
    main()
