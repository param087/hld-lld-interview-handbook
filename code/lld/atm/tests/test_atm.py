from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, Money, SequentialIdGenerator
from lld.atm.bank import InMemoryBank
from lld.atm.dispenser import CashDispenser, JammingFeeder, NoteFeeder, build_chain
from lld.atm.models import (
    AtmStateError,
    AtmStateName,
    Card,
    CardBlockedError,
    CardStatus,
    DailyLimitExceededError,
    DenominationError,
    DispenserJamError,
    InsufficientFundsError,
    InvalidPinError,
    OutOfCashError,
    SessionTimeoutError,
    TransactionStatus,
    TransactionType,
)
from lld.atm.services import ATM

HUNDRED, FIFTY, TWENTY, TEN = (Money.of(v) for v in ("100.00", "50.00", "20.00", "10.00"))
FULL = {HUNDRED: 10, FIFTY: 10, TWENTY: 10, TEN: 10}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_700_000_000)


def build(clock: FakeClock, balance: str = "1000.00", **account_kwargs: Money) -> tuple[InMemoryBank, Card]:
    bank = InMemoryBank(clock=clock, ids=SequentialIdGenerator("TXN"))
    bank.open_account("ACC-1", "Ada", Money.of(balance), **account_kwargs)
    bank.open_account("ACC-2", "Ada", Money.of("100.00"))
    card = bank.issue_card("CARD-1", "Ada", pin="4321", account_ids=("ACC-1", "ACC-2"))
    return bank, card


def machine(
    bank: InMemoryBank, clock: FakeClock, atm_id: str = "atm-1", feeder: NoteFeeder | None = None
) -> ATM:
    return ATM(atm_id, bank, CashDispenser(FULL, feeder=feeder), clock=clock)


def signed_in(
    bank: InMemoryBank, card: Card, clock: FakeClock, atm_id: str = "atm-1", feeder: NoteFeeder | None = None
) -> ATM:
    atm = machine(bank, clock, atm_id, feeder)
    atm.insert_card(card)
    atm.enter_pin("4321")
    return atm


def test_withdrawal_dispenses_notes_and_debits_the_account_once(clock: FakeClock) -> None:
    bank, card = build(clock)
    atm = signed_in(bank, card, clock)
    receipt = atm.withdraw(Money.of("180.00"))
    assert receipt.notes == {HUNDRED: 1, FIFTY: 1, TWENTY: 1, TEN: 1}
    assert receipt.record.balance_after == Money.of("820.00")
    assert bank.balance("ACC-1") == Money.of("820.00")
    assert atm.state is AtmStateName.AUTHENTICATED
    assert atm.dispenser.total() == Money.of("1620.00")  # 1800 loaded minus 180


def test_three_wrong_pins_block_and_retain_the_card(clock: FakeClock) -> None:
    bank, card = build(clock)
    atm = machine(bank, clock)
    atm.insert_card(card)
    for _ in range(2):
        with pytest.raises(InvalidPinError):
            atm.enter_pin("0000")
    with pytest.raises(CardBlockedError):
        atm.enter_pin("0000")
    assert card.status is CardStatus.RETAINED and atm.state is AtmStateName.IDLE
    with pytest.raises(CardBlockedError):
        atm.insert_card(card)


@pytest.mark.parametrize(
    ("operation", "args"),
    [("withdraw", (Money.of("20.00"),)), ("check_balance", ()), ("mini_statement", ())],
)
def test_operations_are_refused_before_authentication(
    clock: FakeClock, operation: str, args: tuple[object, ...]
) -> None:
    bank, card = build(clock)
    atm = machine(bank, clock)
    with pytest.raises(AtmStateError, match="idle"):
        getattr(atm, operation)(*args)
    atm.insert_card(card)
    with pytest.raises(AtmStateError, match="card_inserted"):
        getattr(atm, operation)(*args)


# --8<-- [start:jam]
def test_a_jam_rolls_the_reservation_back_and_takes_the_machine_out_of_service(
    clock: FakeClock,
) -> None:
    bank, card = build(clock, balance="500.00")
    atm = signed_in(bank, card, clock, feeder=JammingFeeder())
    with pytest.raises(DispenserJamError):
        atm.withdraw(Money.of("100.00"))

    assert bank.balance("ACC-1") == Money.of("500.00")  # money promised, never debited
    assert bank.reserved("ACC-1") == Money(0)  # and the promise was released
    assert atm.dispenser.total() == Money.of("1800.00")  # no note left the cassettes
    assert atm.state is AtmStateName.OUT_OF_SERVICE
    ledger = bank.statement("ACC-1", limit=5)
    assert ledger[-1].status is TransactionStatus.ROLLED_BACK
    assert atm.replenish({HUNDRED: 5}) == Money.of("2300.00")
    assert atm.state is AtmStateName.IDLE


# --8<-- [end:jam]


@pytest.mark.parametrize(
    ("amount", "inventory", "expected"),
    [
        ("180.00", FULL, {HUNDRED: 1, FIFTY: 1, TWENTY: 1, TEN: 1}),
        ("60.00", {FIFTY: 1, TWENTY: 3}, {TWENTY: 3}),  # greedy would take the 50 and fail
        ("30.00", {HUNDRED: 5, TEN: 3}, {TEN: 3}),
        ("25.00", FULL, None),  # not a multiple of the smallest note
        ("30.00", {FIFTY: 2, TWENTY: 1}, None),  # 120 in the machine, but not 30 of it
    ],
)
def test_denomination_chain_backtracks_instead_of_being_greedy(
    amount: str, inventory: dict[Money, int], expected: dict[Money, int] | None
) -> None:
    chain = build_chain(tuple(inventory))
    if expected is None:
        with pytest.raises(DenominationError):
            chain.plan(Money.of(amount), inventory)
        return
    assert chain.plan(Money.of(amount), inventory) == expected


def test_daily_limit_is_shared_by_every_machine(clock: FakeClock) -> None:
    bank, card = build(clock, balance="5000.00", daily_limit=Money.of("500.00"))
    first = signed_in(bank, card, clock, atm_id="atm-1")
    first.withdraw(Money.of("400.00"))
    first.cancel()

    second = signed_in(bank, card, clock, atm_id="atm-2")
    with pytest.raises(DailyLimitExceededError, match="100.00"):
        second.withdraw(Money.of("200.00"))
    second.withdraw(Money.of("100.00"))

    clock.advance(86_400)  # a new day - and a session that timed out long ago
    with pytest.raises(SessionTimeoutError):
        second.withdraw(Money.of("300.00"))
    third = signed_in(bank, card, clock, atm_id="atm-3")
    assert third.withdraw(Money.of("300.00")).record.amount == Money.of("300.00")


def test_out_of_cash_is_detected_before_anything_is_reserved(clock: FakeClock) -> None:
    bank, card = build(clock)
    atm = ATM("atm-1", bank, CashDispenser({TWENTY: 2}), clock=clock)
    atm.insert_card(card)
    atm.enter_pin("4321")
    with pytest.raises(OutOfCashError):
        atm.withdraw(Money.of("100.00"))
    assert bank.reserved("ACC-1") == Money(0)
    assert atm.state is AtmStateName.AUTHENTICATED  # a refusal keeps the session open


def test_session_times_out_and_returns_the_card(clock: FakeClock) -> None:
    bank, card = build(clock)
    atm = signed_in(bank, card, clock)
    clock.advance(ATM.SESSION_TIMEOUT_SECONDS + 1)
    with pytest.raises(SessionTimeoutError):
        atm.check_balance()
    assert atm.state is AtmStateName.IDLE and atm.reader.card is None


def test_transfer_records_both_sides_in_a_fixed_lock_order(clock: FakeClock) -> None:
    bank, card = build(clock)
    atm = signed_in(bank, card, clock)
    receipt = atm.transfer("ACC-2", Money.of("250.00"))
    assert receipt.record.type is TransactionType.TRANSFER
    assert bank.balance("ACC-1") == Money.of("750.00")
    assert bank.balance("ACC-2") == Money.of("350.00")
    assert bank.statement("ACC-2", limit=1)[0].counterparty == "ACC-1"
    with pytest.raises(InsufficientFundsError):
        atm.transfer("ACC-2", Money.of("10000.00"))


# --8<-- [start:concurrency]
def test_two_machines_never_overdraw_the_same_account(clock: FakeClock) -> None:
    # 500 in the account, a limit high enough not to interfere, 16 attempts of 100.
    bank, card = build(clock, balance="500.00", daily_limit=Money.of("5000.00"))
    second_card = bank.issue_card("CARD-2", "Ada", pin="4321", account_ids=("ACC-1",))
    atms = [
        signed_in(bank, card, clock, atm_id="atm-1"),
        signed_in(bank, second_card, clock, atm_id="atm-2"),
    ]

    def withdraw(i: int) -> Money | None:
        try:
            return atms[i % 2].withdraw(Money.of("100.00")).record.amount
        except InsufficientFundsError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(withdraw, range(16)))

    assert sum(1 for r in results if r is not None) == 5  # 500 / 100, not one note more
    assert bank.balance("ACC-1") == Money(0)
    assert bank.reserved("ACC-1") == Money(0)


# --8<-- [end:concurrency]
