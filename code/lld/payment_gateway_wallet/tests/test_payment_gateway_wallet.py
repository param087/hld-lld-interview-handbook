from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, Money, SequentialIdGenerator
from lld.payment_gateway_wallet.fraud import (
    AmountCeilingRule,
    DailyLimitRule,
    DenylistRule,
    FraudContext,
    FraudRule,
    VelocityRule,
    build_default_chain,
)
from lld.payment_gateway_wallet.ledger import (
    FEES,
    PSP_CLEARING,
    Ledger,
    LedgerEntry,
    wallet_account,
)
from lld.payment_gateway_wallet.models import (
    EntryDirection,
    FraudRejectedError,
    IdempotencyConflictError,
    InsufficientBalanceError,
    LedgerImbalanceError,
    PaymentDeclinedError,
    PaymentMethod,
    PaymentMethodType,
    Transaction,
    TransactionStatus,
    TransactionType,
    WebhookEvent,
)
from lld.payment_gateway_wallet.psp import PaymentProcessorFactory
from lld.payment_gateway_wallet.services import PaymentService, TransactionLog, WalletService
from lld.payment_gateway_wallet.store import PaymentStore
from lld.payment_gateway_wallet.webhooks import WebhookHandler

CARD = PaymentMethod("pm-1", "ada", PaymentMethodType.CARD, "tok_live", "Visa 4242")
DEAD_CARD = PaymentMethod("pm-2", "ada", PaymentMethodType.CARD, "tok_dead", "Visa 0000")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000)


class Rig:
    """Everything wired together, the way ``main`` would do it."""

    def __init__(self, clock: FakeClock, ceiling: str = "500.00", daily_cap: str = "800.00") -> None:
        self.store = PaymentStore()
        self.log = TransactionLog()
        self.webhooks = WebhookHandler(self.store, clock=clock, ids=SequentialIdGenerator("L"))
        self.wallets = WalletService(
            self.store, PaymentProcessorFactory.with_stubs(declined_tokens={"tok_dead"}),
            clock=clock, ids=SequentialIdGenerator("T"), listeners=[self.log], on_reference=self.webhooks.replay,
        )
        self.payments = PaymentService(
            self.store, build_default_chain(Money.of(ceiling), Money.of(daily_cap)),
            clock=clock, ids=SequentialIdGenerator("P"), listeners=[self.log],
        )


def test_a_transfer_moves_money_and_keeps_the_books_balanced(clock: FakeClock) -> None:
    rig = Rig(clock)
    ada = rig.wallets.open_wallet("ada", Money.of("200.00"))
    bob = rig.wallets.open_wallet("bob", Money.of("50.00"))
    transaction = rig.wallets.transfer("k1", ada.id, bob.id, Money.of("75.00"))

    assert transaction.status is TransactionStatus.CAPTURED
    assert rig.wallets.balance(ada.id) == Money.of("125.00")
    assert rig.wallets.balance(bob.id) == Money.of("125.00")
    assert rig.store.ledger.is_balanced()
    assert rig.store.ledger.balance(wallet_account(ada.id)) == rig.wallets.balance(ada.id)
    assert len(rig.store.ledger.entries_for(transaction.id)) == 2
    assert [t.id for t in rig.log.all()] == [transaction.id]


# --8<-- [start:idempotency]
def test_a_repeated_idempotency_key_returns_the_stored_result(clock: FakeClock) -> None:
    rig = Rig(clock)
    ada = rig.wallets.open_wallet("ada", Money.of("200.00"))
    bob = rig.wallets.open_wallet("bob", Money.of("0.00"))

    first = rig.wallets.transfer("k1", ada.id, bob.id, Money.of("30.00"))
    replayed = rig.wallets.transfer("k1", ada.id, bob.id, Money.of("30.00"))

    assert replayed.id == first.id  # the stored transaction, not a second one
    assert rig.wallets.balance(ada.id) == Money.of("170.00")  # debited exactly once
    assert rig.store.ledger.size() == 4  # two opening entries plus one transfer posting

    with pytest.raises(IdempotencyConflictError, match="different request"):
        rig.wallets.transfer("k1", ada.id, bob.id, Money.of("31.00"))


# --8<-- [end:idempotency]


def test_a_wallet_cannot_go_negative_and_the_failed_call_leaves_no_trace(clock: FakeClock) -> None:
    rig = Rig(clock)
    ada = rig.wallets.open_wallet("ada", Money.of("40.00"))
    bob = rig.wallets.open_wallet("bob", Money.of("0.00"))
    entries_before = rig.store.ledger.size()

    with pytest.raises(InsufficientBalanceError):
        rig.wallets.transfer("k1", ada.id, bob.id, Money.of("41.00"))

    assert rig.wallets.balance(ada.id) == Money.of("40.00")
    assert rig.store.ledger.size() == entries_before
    # the key was released, so the client can retry after topping up
    rig.wallets.transfer("k1", ada.id, bob.id, Money.of("10.00"))
    assert rig.wallets.balance(bob.id) == Money.of("10.00")


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        (AmountCeilingRule(Money.of("100.00")), "amount_ceiling"),
        (DenylistRule(["merchant:cafe"]), "denylist"),
        (VelocityRule(1, 60.0), "velocity"),
        (DailyLimitRule(Money.of("150.00")), "daily_limit"),
    ],
)
def test_every_fraud_rule_blocks_with_its_own_reason(rule: FraudRule, expected: str) -> None:
    history = (
        Transaction(
            "T-0", TransactionType.MERCHANT_PAYMENT, TransactionStatus.CAPTURED, Money.of("140.00"),
            "wallet:w1", "merchant:cafe", "k0", 990.0,
        ),
    )
    context = FraudContext("w1", Money.of("200.00"), "merchant:cafe", 1_000.0, history)
    decision = rule.evaluate(context)
    assert not decision.allowed and decision.rule == expected


def test_the_chain_records_a_block_as_a_failed_transaction(clock: FakeClock) -> None:
    rig = Rig(clock, ceiling="100.00")
    ada = rig.wallets.open_wallet("ada", Money.of("500.00"))
    with pytest.raises(FraudRejectedError, match="amount_ceiling"):
        rig.payments.pay_merchant("k1", ada.id, "cafe", Money.of("300.00"))
    assert rig.wallets.balance(ada.id) == Money.of("500.00")
    blocked = [t for t in rig.wallets.history(ada.id) if t.status is TransactionStatus.FAILED]
    assert len(blocked) == 1 and rig.store.ledger.is_balanced()
    with pytest.raises(FraudRejectedError):  # the replay re-raises the stored failure
        rig.payments.pay_merchant("k1", ada.id, "cafe", Money.of("300.00"))


def test_partial_then_full_refund_walks_the_state_machine(clock: FakeClock) -> None:
    rig = Rig(clock)
    ada = rig.wallets.open_wallet("ada", Money.of("500.00"))
    payment = rig.payments.pay_merchant("k1", ada.id, "cafe", Money.of("100.00"))
    assert rig.store.ledger.balance(FEES) == Money.of("2.00")

    rig.payments.refund("r1", payment.id, Money.of("40.00"))
    assert rig.store.transaction(payment.id).status is TransactionStatus.PARTIALLY_REFUNDED
    rig.payments.refund("r2", payment.id)  # the rest
    settled = rig.store.transaction(payment.id)
    assert settled.status is TransactionStatus.REFUNDED and settled.refunded == Money.of("100.00")
    assert rig.wallets.balance(ada.id) == Money.of("500.00")
    assert rig.store.ledger.balance(FEES) == Money(0) and rig.store.ledger.is_balanced()
    with pytest.raises(Exception, match="can refund at most"):
        rig.payments.refund("r3", payment.id, Money.of("1.00"))


# --8<-- [start:concurrency]
def test_concurrent_transfers_in_both_directions_conserve_money_and_never_deadlock(clock: FakeClock) -> None:
    rig = Rig(clock)
    ada = rig.wallets.open_wallet("ada", Money.of("100.00"))
    bob = rig.wallets.open_wallet("bob", Money.of("100.00"))
    total_before = rig.wallets.balance(ada.id) + rig.wallets.balance(bob.id)

    def move(i: int) -> bool:
        source, target = (ada.id, bob.id) if i % 2 == 0 else (bob.id, ada.id)
        try:
            rig.wallets.transfer(f"k{i}", source, target, Money.of("7.00"))
        except InsufficientBalanceError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(move, range(40)))

    ada_balance, bob_balance = rig.wallets.balance(ada.id), rig.wallets.balance(bob.id)
    assert ada_balance + bob_balance == total_before  # no cent created or destroyed
    assert ada_balance.cents >= 0 and bob_balance.cents >= 0
    assert rig.store.ledger.is_balanced()
    assert rig.store.ledger.size() == 4 + 2 * results.count(True)  # 2 openings + 2 per transfer


# --8<-- [end:concurrency]


# --8<-- [start:webhooks]
def test_webhooks_are_deduplicated_ordered_and_replayed(clock: FakeClock) -> None:
    rig = Rig(clock)
    ada = rig.wallets.open_wallet("ada", Money.of("0.00"))

    early = WebhookEvent("evt-early", "auth_0002", TransactionStatus.CAPTURED, clock.now())
    assert rig.webhooks.handle(early) == "deferred"  # we have not authorized this yet

    top_up = rig.wallets.top_up("k1", ada.id, CARD, Money.of("100.00"))
    reference = top_up.psp_reference or ""
    assert top_up.status is TransactionStatus.AUTHORIZED
    assert rig.wallets.balance(ada.id) == Money(0)  # authorization is not settlement

    assert rig.webhooks.handle(WebhookEvent("evt-1", reference, TransactionStatus.CAPTURED, clock.now())) == "applied"
    assert rig.wallets.balance(ada.id) == Money.of("100.00")
    assert rig.webhooks.handle(WebhookEvent("evt-1", reference, TransactionStatus.CAPTURED, clock.now())) == "duplicate"
    assert rig.webhooks.handle(WebhookEvent("evt-2", reference, TransactionStatus.AUTHORIZED, clock.now())) == "ignored"
    assert rig.wallets.balance(ada.id) == Money.of("100.00")  # neither moved money again

    second = rig.wallets.top_up("k2", ada.id, CARD, Money.of("60.00"))  # reference auth_0002
    assert rig.webhooks.parked() == 0  # the early event was replayed on commit
    assert rig.store.transaction(second.id).status is TransactionStatus.CAPTURED
    assert rig.wallets.balance(ada.id) == Money.of("160.00")
    assert rig.store.ledger.is_balanced()


# --8<-- [end:webhooks]


def test_a_declined_processor_releases_the_reservation(clock: FakeClock) -> None:
    rig = Rig(clock)
    ada = rig.wallets.open_wallet("ada", Money.of("100.00"))
    with pytest.raises(PaymentDeclinedError):
        rig.wallets.withdraw("k1", ada.id, DEAD_CARD, Money.of("30.00"))
    wallet = rig.store.wallet(ada.id)
    assert wallet.balance == Money.of("100.00") and wallet.reserved == Money(0)
    assert rig.store.ledger.is_balanced()

    settled = rig.wallets.withdraw("k2", ada.id, CARD, Money.of("30.00"))
    assert settled.status is TransactionStatus.CAPTURED
    assert rig.wallets.balance(ada.id) == Money.of("70.00")
    assert rig.store.ledger.balance(PSP_CLEARING) == Money.of("-70.00")  # 100 out, 30 back in


def test_the_ledger_refuses_an_imbalanced_posting() -> None:
    ledger = Ledger()
    lopsided = [
        LedgerEntry("l1", "T-1", "wallet:w1", EntryDirection.DEBIT, Money.of("10.00"), 0.0),
        LedgerEntry("l2", "T-1", "wallet:w2", EntryDirection.CREDIT, Money.of("9.99"), 0.0),
    ]
    with pytest.raises(LedgerImbalanceError, match="out by -1 cents"):
        ledger.post(lopsided)
    assert ledger.size() == 0 and ledger.is_balanced()


@pytest.mark.parametrize(
    ("method_type", "token", "approved"),
    [
        (PaymentMethodType.CARD, "tok_live", True),
        (PaymentMethodType.CARD, "tok_dead", False),
        (PaymentMethodType.UPI, "ada@bank", True),
        (PaymentMethodType.UPI, "tok_dead", False),
        (PaymentMethodType.NETBANKING, "acct-1", True),
        (PaymentMethodType.NETBANKING, "tok_dead", False),
    ],
)
def test_every_adapter_normalises_its_vendor_response(
    method_type: PaymentMethodType, token: str, approved: bool
) -> None:
    factory = PaymentProcessorFactory.with_stubs(declined_tokens={"tok_dead"})
    method = PaymentMethod("pm", "ada", method_type, token, "test")
    result = factory.for_method(method).authorize(method, Money.of("25.00"), "T-1")
    assert result.approved is approved
    assert bool(result.reference) is approved
