"""One day of a wallet: top-up by card, webhooks, a transfer, a payment, a refund."""

from common import FakeClock, Money, SequentialIdGenerator
from lld.payment_gateway_wallet.fraud import build_default_chain
from lld.payment_gateway_wallet.ledger import FEES, merchant_account, wallet_account
from lld.payment_gateway_wallet.models import (
    FraudRejectedError,
    IdempotencyConflictError,
    PaymentMethod,
    PaymentMethodType,
    TransactionStatus,
    WebhookEvent,
)
from lld.payment_gateway_wallet.psp import PaymentProcessorFactory
from lld.payment_gateway_wallet.services import PaymentService, WalletService
from lld.payment_gateway_wallet.store import PaymentStore
from lld.payment_gateway_wallet.webhooks import WebhookHandler


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    store = PaymentStore()
    webhooks = WebhookHandler(store, clock=clock, ids=SequentialIdGenerator("L"))
    wallets = WalletService(
        store, PaymentProcessorFactory.with_stubs(declined_tokens={"tok_dead"}),
        clock=clock, ids=SequentialIdGenerator("T"), on_reference=webhooks.replay,
    )
    payments = PaymentService(
        store, build_default_chain(ceiling=Money.of("500.00"), daily_cap=Money.of("800.00")),
        clock=clock, ids=SequentialIdGenerator("P"),
    )
    card = PaymentMethod("pm-1", "ada", PaymentMethodType.CARD, "tok_live", "Visa 4242")
    ada = wallets.open_wallet("ada", Money.of("200.00"))
    bob = wallets.open_wallet("bob", Money.of("50.00"))
    print(f"opened {ada.id} with {ada.balance} and {bob.id} with {bob.balance}")

    top_up = wallets.top_up("idem-topup-1", ada.id, card, Money.of("300.00"))
    print(f"top-up {top_up.id} {top_up.status} at the card network, balance still {wallets.balance(ada.id)}")
    print(f"capture webhook: {webhooks.handle(WebhookEvent('evt-1', top_up.psp_reference or '', TransactionStatus.CAPTURED, clock.now()))}, balance {wallets.balance(ada.id)}")
    print(f"same webhook again: {webhooks.handle(WebhookEvent('evt-1', top_up.psp_reference or '', TransactionStatus.CAPTURED, clock.now()))}")
    print(f"late authorized webhook: {webhooks.handle(WebhookEvent('evt-2', top_up.psp_reference or '', TransactionStatus.AUTHORIZED, clock.now()))}")

    early = WebhookEvent("evt-3", "auth_0002", TransactionStatus.CAPTURED, clock.now())
    print(f"webhook for the next authorization arrives first: {webhooks.handle(early)}, parked {webhooks.parked()}")
    second = wallets.top_up("idem-topup-2", ada.id, card, Money.of("100.00"))
    print(f"top-up {second.id} committed, parked drained to {webhooks.parked()}, balance {wallets.balance(ada.id)}")

    transfer = wallets.transfer("idem-xfer-1", ada.id, bob.id, Money.of("120.00"))
    print(f"transfer {transfer.id}: ada {wallets.balance(ada.id)}, bob {wallets.balance(bob.id)}")
    print(f"retry of the same key: {wallets.transfer('idem-xfer-1', ada.id, bob.id, Money.of('120.00')).id}, ada still {wallets.balance(ada.id)}")
    try:
        wallets.transfer("idem-xfer-1", ada.id, bob.id, Money.of("5.00"))
    except IdempotencyConflictError as exc:
        print(f"same key, different amount: {exc}")

    payment = payments.pay_merchant("idem-pay-1", ada.id, "cafe", Money.of("60.00"))
    fee = PaymentService.fee_for(payment.amount)
    print(f"paid cafe {payment.amount}: merchant {store.ledger.balance(merchant_account('cafe'))}, fees {store.ledger.balance(FEES)} (2% of {payment.amount} is {fee})")
    refund = payments.refund("idem-ref-1", payment.id, Money.of("25.00"))
    print(f"refund {refund.amount}: transaction now {store.transaction(payment.id).status}, ada {wallets.balance(ada.id)}")
    try:
        payments.pay_merchant("idem-pay-2", ada.id, "cafe", Money.of("600.00"))
    except FraudRejectedError as exc:
        print(f"fraud chain: {exc}")
    print(f"ledger balanced: {store.ledger.is_balanced()} over {store.ledger.size()} entries")
    print(f"wallet ledger account matches the wallet: {store.ledger.balance(wallet_account(ada.id))} == {wallets.balance(ada.id)}")


if __name__ == "__main__":
    main()
