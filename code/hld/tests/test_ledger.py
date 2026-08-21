from concurrent.futures import ThreadPoolExecutor

import pytest

from common import (
    ConflictError,
    FakeClock,
    InvalidStateError,
    Money,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)
from hld.ledger import (
    AccountType,
    Entry,
    Ledger,
    Payment,
    PaymentState,
    PaymentStateMachine,
    SettlementRow,
    reconcile,
)


@pytest.fixture
def ledger() -> Ledger:
    book = Ledger(FakeClock(start=1_000.0), SequentialIdGenerator("txn"))
    book.open_account("cash:provider", AccountType.ASSET, allow_negative=True)
    book.open_account("wallet:ann", AccountType.LIABILITY)
    book.open_account("wallet:bob", AccountType.LIABILITY)
    book.open_account("revenue:fees", AccountType.REVENUE)
    return book


def top_up(book: Ledger, account: str, amount: str, key: str = "topup-1") -> None:
    money = Money.of(amount)
    book.post(key, [Entry.debit("cash:provider", money), Entry.credit(account, money)])


def test_posting_balances_and_moves_money_in_natural_signs(ledger: Ledger) -> None:
    top_up(ledger, "wallet:ann", "50.00")
    assert ledger.balance("wallet:ann") == Money.of("50.00")
    assert ledger.balance("cash:provider") == Money.of("50.00")  # an asset grows with debits
    assert ledger.trial_balance() == 0
    ledger.post(
        "transfer-1",
        [
            Entry.debit("wallet:ann", Money.of("12.34")),
            Entry.credit("wallet:bob", Money.of("12.09")),
            Entry.credit("revenue:fees", Money.of("0.25")),
        ],
        memo="ann pays bob",
    )
    assert ledger.balance("wallet:ann") == Money.of("37.66")
    assert ledger.balance("wallet:bob") == Money.of("12.09")
    assert ledger.balance("revenue:fees") == Money.of("0.25")
    assert ledger.trial_balance() == 0  # the invariant that must hold after every write
    assert [txn for txn, _ in ledger.statement("wallet:ann")] == ["txn-1", "txn-2"]


def test_unbalanced_and_malformed_transactions_are_refused(ledger: Ledger) -> None:
    ten, nine = Money.of("10.00"), Money.of("9.00")
    with pytest.raises(ValidationError, match="does not balance"):
        ledger.post("bad-1", [Entry.debit("cash:provider", ten), Entry.credit("wallet:ann", nine)])
    with pytest.raises(ValidationError, match="at least two entries"):
        ledger.post("bad-2", [Entry.debit("cash:provider", ten)])
    with pytest.raises(ValidationError, match="one currency"):
        ledger.post(
            "bad-3",
            [Entry.debit("cash:provider", ten), Entry.credit("wallet:ann", Money.of("10.00", "EUR"))],
        )
    with pytest.raises(ValidationError, match="positive"):
        Entry.debit("cash:provider", Money.of("-1.00"))
    with pytest.raises(NotFoundError):
        ledger.post("bad-4", [Entry.debit("wallet:zoe", ten), Entry.credit("wallet:ann", ten)])
    assert ledger.balance("wallet:ann").is_zero()  # nothing partially applied
    assert ledger.trial_balance() == 0


def test_idempotency_key_replays_instead_of_moving_money_twice(ledger: Ledger) -> None:
    money = Money.of("25.00")
    entries = [Entry.debit("cash:provider", money), Entry.credit("wallet:ann", money)]
    first = ledger.post("topup-ann-1", entries)
    second = ledger.post("topup-ann-1", entries)
    assert first is second
    assert ledger.balance("wallet:ann") == money
    assert len(ledger.statement("wallet:ann")) == 1


def test_optimistic_version_check_rejects_the_slower_writer(ledger: Ledger) -> None:
    top_up(ledger, "wallet:ann", "10.00")
    stale = ledger.version("wallet:ann")
    one = Money.of("1.00")
    ledger.post(
        "transfer-1",
        [Entry.debit("wallet:ann", one), Entry.credit("wallet:bob", one)],
        expected_versions={"wallet:ann": stale},
    )
    with pytest.raises(ConflictError, match="stale account version"):
        ledger.post(
            "transfer-2",
            [Entry.debit("wallet:ann", one), Entry.credit("wallet:bob", one)],
            expected_versions={"wallet:ann": stale},
        )
    assert ledger.balance("wallet:ann") == Money.of("9.00")  # the loser wrote nothing
    ledger.post(  # re-read the version and retry: the standard optimistic loop
        "transfer-2",
        [Entry.debit("wallet:ann", one), Entry.credit("wallet:bob", one)],
        expected_versions={"wallet:ann": ledger.version("wallet:ann")},
    )
    assert ledger.balance("wallet:bob") == Money.of("2.00")


def test_a_wallet_cannot_go_overdrawn_but_a_provider_account_can(ledger: Ledger) -> None:
    top_up(ledger, "wallet:ann", "5.00")
    with pytest.raises(ConflictError, match="would go to"):
        ledger.post(
            "transfer-1",
            [Entry.debit("wallet:ann", Money.of("5.01")), Entry.credit("wallet:bob", Money.of("5.01"))],
        )
    assert ledger.balance("wallet:ann") == Money.of("5.00")
    assert ledger.balance("cash:provider") == Money.of("5.00")
    ledger.post(  # a payout drains the provider account below zero, which is allowed
        "payout-1",
        [Entry.debit("wallet:ann", Money.of("5.00")), Entry.credit("cash:provider", Money.of("5.00"))],
    )
    assert ledger.balance("cash:provider").is_zero()
    assert ledger.trial_balance() == 0


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([("e1", PaymentState.AUTHORIZED)], PaymentState.AUTHORIZED),
        ([("e1", PaymentState.AUTHORIZED), ("e2", PaymentState.CAPTURED)], PaymentState.CAPTURED),
        ([("e1", PaymentState.FAILED)], PaymentState.FAILED),
        (
            [("e1", PaymentState.AUTHORIZED), ("e2", PaymentState.CAPTURED), ("e3", PaymentState.SETTLED)],
            PaymentState.SETTLED,
        ),
    ],
)
def test_payment_state_machine_walks_the_happy_paths(
    events: list[tuple[str, PaymentState]], expected: PaymentState
) -> None:
    machine, payment = PaymentStateMachine(), Payment("pay-1", Money.of("10.00"))
    for event_id, target in events:
        assert machine.apply(payment, event_id, target) is True
    assert payment.state is expected


def test_webhooks_may_duplicate_and_arrive_out_of_order() -> None:
    machine, payment = PaymentStateMachine(), Payment("pay-1", Money.of("10.00"))
    assert machine.apply(payment, "e1", PaymentState.AUTHORIZED) is True
    assert machine.apply(payment, "e1", PaymentState.AUTHORIZED) is False  # provider retried
    assert machine.apply(payment, "e2", PaymentState.CAPTURED) is True
    assert machine.apply(payment, "e3", PaymentState.AUTHORIZED) is False  # stale, already implied
    assert machine.apply(payment, "e4", PaymentState.FAILED) is False  # the failed auth attempt
    assert payment.state is PaymentState.CAPTURED
    with pytest.raises(InvalidStateError, match="cannot go"):
        machine.apply(Payment("pay-2", Money.of("1.00")), "e5", PaymentState.SETTLED)


def test_reconcile_splits_the_difference_into_actionable_buckets() -> None:
    ours = {"pay-1": Money.of("12.34"), "pay-2": Money.of("40.00"), "pay-3": Money.of("7.50")}
    theirs = [
        SettlementRow("pay-1", Money.of("12.34")),
        SettlementRow("pay-2", Money.of("39.10")),
        SettlementRow("pay-9", Money.of("5.00")),
    ]
    report = reconcile(ours, theirs)
    assert report.matched == ("pay-1",)
    assert report.amount_mismatch == (("pay-2", Money.of("40.00"), Money.of("39.10")),)
    assert report.missing_at_provider == ("pay-3",)
    assert report.missing_in_ledger == ("pay-9",)
    assert report.is_clean is False
    assert reconcile({"pay-1": Money.of("1.00")}, [SettlementRow("pay-1", Money.of("1.00"))]).is_clean


def test_concurrent_transfers_never_overdraw_and_keep_the_ledger_balanced(ledger: Ledger) -> None:
    top_up(ledger, "wallet:ann", "1.00")  # exactly 100 cents to give away
    cent = Money(1)

    def transfer(i: int) -> bool:
        try:
            ledger.post(
                f"transfer-{i}",
                [Entry.debit("wallet:ann", cent), Entry.credit("wallet:bob", cent)],
            )
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(transfer, range(200)))
    assert results.count(True) == 100
    assert ledger.balance("wallet:ann").is_zero()
    assert ledger.balance("wallet:bob") == Money.of("1.00")
    assert ledger.trial_balance() == 0


def test_concurrent_retries_of_one_idempotency_key_post_once(ledger: Ledger) -> None:
    money = Money.of("30.00")
    entries = [Entry.debit("cash:provider", money), Entry.credit("wallet:ann", money)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        txns = list(pool.map(lambda _: ledger.post("topup-ann-1", entries), range(50)))
    assert len({txn.txn_id for txn in txns}) == 1
    assert ledger.balance("wallet:ann") == money
    assert ledger.trial_balance() == 0
