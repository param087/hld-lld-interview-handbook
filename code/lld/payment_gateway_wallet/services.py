"""``WalletService`` and ``PaymentService``: every money-moving call in the system.

The shape is identical in all of them and comes from ``MoneyService``: claim the
idempotency key, take the wallet locks in id order, do the work inside a
``PaymentUnitOfWork``, commit, complete the key. A call to a processor happens
between two transactions, never while a lock is held.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Protocol

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock, ValidationError
from lld.payment_gateway_wallet.fraud import FraudContext, FraudRule
from lld.payment_gateway_wallet.ledger import (
    FEES,
    PSP_CLEARING,
    LedgerEntry,
    merchant_account,
    wallet_account,
    wallet_id_of,
)
from lld.payment_gateway_wallet.models import (
    EntryDirection,
    FraudRejectedError,
    PaymentDeclinedError,
    PaymentMethod,
    Refund,
    Transaction,
    TransactionStatus,
    TransactionType,
    Wallet,
)
from lld.payment_gateway_wallet.psp import PaymentProcessorFactory
from lld.payment_gateway_wallet.store import IdempotencyRecord, PaymentStore, PaymentUnitOfWork

MERCHANT_FEE_BASIS_POINTS = 200  # 2.00%, charged to the merchant, kept by the platform


# --8<-- [start:listener]
class TransactionListener(Protocol):
    """Observer of settled transactions: receipts, notifications, analytics."""

    def on_transaction(self, transaction: Transaction) -> None: ...


class TransactionLog:
    """The simplest listener: an append-only list, safe to read from another thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: list[Transaction] = []

    def on_transaction(self, transaction: Transaction) -> None:
        with self._lock:
            self._seen.append(transaction)

    def all(self) -> list[Transaction]:
        with self._lock:
            return list(self._seen)


# --8<-- [end:listener]


# --8<-- [start:base]
class MoneyService:
    """Template Method: the steps every money-moving operation shares.

    Subclasses supply the middle of the sandwich; this class owns id and clock
    injection, ledger-entry construction, idempotent replay and notification.
    """

    def __init__(
        self,
        store: PaymentStore,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        listeners: Iterable[TransactionListener] = (),
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("T")
        self._listeners = list(listeners)

    def _debit(self, transaction_id: str, account: str, amount: Money) -> LedgerEntry:
        return LedgerEntry(
            self._ids.next_id(), transaction_id, account, EntryDirection.DEBIT, amount, self._clock.now()
        )

    def _credit(self, transaction_id: str, account: str, amount: Money) -> LedgerEntry:
        return LedgerEntry(
            self._ids.next_id(), transaction_id, account, EntryDirection.CREDIT, amount, self._clock.now()
        )

    def _new(
        self,
        kind: TransactionType,
        amount: Money,
        source: str,
        destination: str,
        idempotency_key: str,
        status: TransactionStatus = TransactionStatus.INITIATED,
    ) -> Transaction:
        if amount.cents <= 0:
            raise ValidationError(f"amount must be positive, got {amount}")
        return Transaction(
            self._ids.next_id(), kind, status, amount, source, destination, idempotency_key, self._clock.now()
        )

    def _replay(self, record: IdempotencyRecord) -> Transaction:
        """A repeated key returns the stored outcome, and re-raises a stored failure."""
        transaction = self._store.transaction(record.transaction_id or "")
        if transaction.status is TransactionStatus.FAILED:
            reason = transaction.failure_reason or "declined"
            error = FraudRejectedError if reason.startswith("fraud:") else PaymentDeclinedError
            raise error(f"{transaction.id} already failed: {reason}")
        return transaction

    def _finish(self, idempotency_key: str, transaction_id: str, failure: type[Exception]) -> Transaction:
        """Complete the key, then surface a business failure the same way a replay does."""
        self._store.idempotency.complete(idempotency_key, transaction_id)
        settled = self._store.transaction(transaction_id)
        if settled.status is TransactionStatus.FAILED:
            raise failure(f"{settled.id} failed: {settled.failure_reason}")
        self._notify(settled)
        return settled

    def _notify(self, transaction: Transaction) -> None:
        for listener in self._listeners:
            listener.on_transaction(transaction)  # outside every lock


# --8<-- [end:base]


# --8<-- [start:wallet_service]
class WalletService(MoneyService):
    """Opening, funding, withdrawing and wallet-to-wallet transfers."""

    def __init__(
        self,
        store: PaymentStore,
        processors: PaymentProcessorFactory,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        listeners: Iterable[TransactionListener] = (),
        on_reference: Callable[[str], object] | None = None,
    ) -> None:
        super().__init__(store, clock, ids, listeners)
        self._processors = processors
        # Wired to ``WebhookHandler.replay``: drains events that arrived before
        # this transaction's processor reference was committed.
        self._on_reference = on_reference

    def open_wallet(self, owner_id: str, opening_balance: Money) -> Wallet:
        """The opening balance is funded from the clearing account, so the book balances."""
        wallet = Wallet(self._ids.next_id(), owner_id, Money(0, opening_balance.currency))
        self._store.open_wallet(wallet)
        if opening_balance.cents == 0:
            return wallet
        transaction = self._new(
            TransactionType.TOP_UP, opening_balance, PSP_CLEARING, wallet_account(wallet.id),
            f"open:{wallet.id}", TransactionStatus.CAPTURED,
        )
        with self._store.locked(wallet.id), PaymentUnitOfWork(self._store, [wallet.id]) as uow:
            uow.wallet(wallet.id).credit(opening_balance)
            uow.track(transaction)
            uow.post(
                self._debit(transaction.id, PSP_CLEARING, opening_balance),
                self._credit(transaction.id, wallet_account(wallet.id), opening_balance),
            )
            uow.commit()
        return self._store.wallet(wallet.id)

    def top_up(self, idempotency_key: str, wallet_id: str, method: PaymentMethod, amount: Money) -> Transaction:
        """Authorize with the processor; the money lands when the capture webhook arrives."""
        claimed = self._store.idempotency.claim(
            idempotency_key, f"top_up:{wallet_id}:{amount.cents}:{method.id}"
        )
        if claimed is not None:
            return self._replay(claimed)
        try:
            transaction = self._new(
                TransactionType.TOP_UP, amount, PSP_CLEARING, wallet_account(wallet_id), idempotency_key
            )
            with self._store.locked(wallet_id), PaymentUnitOfWork(self._store, [wallet_id]) as uow:
                uow.track(transaction)
                uow.commit()
            result = self._processors.for_method(method).authorize(method, amount, transaction.id)
            with self._store.locked(wallet_id), PaymentUnitOfWork(self._store, [wallet_id], [transaction.id]) as uow:
                current = uow.transaction(transaction.id)
                if result.approved:
                    current.psp_reference = result.reference
                    current.transition_to(TransactionStatus.AUTHORIZED)
                else:
                    current.failure_reason = f"psp:{result.code}"
                    current.transition_to(TransactionStatus.FAILED)
                uow.commit()
        except Exception:
            self._store.idempotency.release(idempotency_key)
            raise
        settled = self._finish(idempotency_key, transaction.id, PaymentDeclinedError)
        if self._on_reference is not None and settled.psp_reference:
            self._on_reference(settled.psp_reference)
        return self._store.transaction(transaction.id)

    def withdraw(self, idempotency_key: str, wallet_id: str, method: PaymentMethod, amount: Money) -> Transaction:
        """Reserve, ask the processor, then either settle or hand the reservation back."""
        claimed = self._store.idempotency.claim(
            idempotency_key, f"withdraw:{wallet_id}:{amount.cents}:{method.id}"
        )
        if claimed is not None:
            return self._replay(claimed)
        held = False  # True only while a committed reservation has no matching settlement
        try:
            transaction = self._new(
                TransactionType.WITHDRAWAL, amount, wallet_account(wallet_id), PSP_CLEARING, idempotency_key
            )
            with self._store.locked(wallet_id), PaymentUnitOfWork(self._store, [wallet_id]) as uow:
                uow.wallet(wallet_id).reserve(amount)  # raises before the key is completed
                uow.track(transaction)
                uow.commit()
            held = True
            result = self._processors.for_method(method).authorize(method, amount, transaction.id)
            with self._store.locked(wallet_id), PaymentUnitOfWork(self._store, [wallet_id], [transaction.id]) as uow:
                wallet, current = uow.wallet(wallet_id), uow.transaction(transaction.id)
                wallet.release(amount)
                if result.approved:
                    wallet.debit(amount)
                    current.psp_reference = result.reference
                    current.transition_to(TransactionStatus.CAPTURED)
                    uow.post(
                        self._debit(current.id, wallet_account(wallet_id), amount),
                        self._credit(current.id, PSP_CLEARING, amount),
                    )
                else:
                    current.failure_reason = f"psp:{result.code}"
                    current.transition_to(TransactionStatus.FAILED)
                uow.commit()
            held = False
        except Exception:
            # A rail that raises instead of declining is the path that strands money:
            # the reservation is already committed, so compensate before re-raising.
            if held:
                self._release_reservation(wallet_id, amount)
            self._store.idempotency.release(idempotency_key)
            raise
        return self._finish(idempotency_key, transaction.id, PaymentDeclinedError)

    def _release_reservation(self, wallet_id: str, amount: Money) -> None:
        """Compensating action: give back a hold whose processor call never answered."""
        with self._store.locked(wallet_id), PaymentUnitOfWork(self._store, [wallet_id]) as uow:
            uow.wallet(wallet_id).release(amount)
            uow.commit()

    def transfer(self, idempotency_key: str, source_id: str, target_id: str, amount: Money) -> Transaction:
        """Wallet to wallet. Both locks, always in id order, one transaction, no processor."""
        if source_id == target_id:
            raise ValidationError("cannot transfer to the same wallet")
        claimed = self._store.idempotency.claim(
            idempotency_key, f"transfer:{source_id}:{target_id}:{amount.cents}"
        )
        if claimed is not None:
            return self._replay(claimed)
        try:
            transaction = self._new(
                TransactionType.TRANSFER, amount, wallet_account(source_id), wallet_account(target_id),
                idempotency_key, TransactionStatus.CAPTURED,
            )
            with self._store.locked(source_id, target_id), PaymentUnitOfWork(self._store, [source_id, target_id]) as uow:
                uow.wallet(source_id).debit(amount)
                uow.wallet(target_id).credit(amount)
                uow.track(transaction)
                uow.post(
                    self._debit(transaction.id, wallet_account(source_id), amount),
                    self._credit(transaction.id, wallet_account(target_id), amount),
                )
                uow.commit()
        except Exception:
            self._store.idempotency.release(idempotency_key)
            raise
        return self._finish(idempotency_key, transaction.id, PaymentDeclinedError)

    def balance(self, wallet_id: str) -> Money:
        return self._store.wallet(wallet_id).balance

    def history(self, wallet_id: str) -> list[Transaction]:
        return self._store.history(wallet_id)


# --8<-- [end:wallet_service]


# --8<-- [start:payment_service]
class PaymentService(MoneyService):
    """Merchant payments and refunds: the fraud chain, the fee split and the reversal."""

    def __init__(
        self,
        store: PaymentStore,
        fraud_chain: FraudRule,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        listeners: Iterable[TransactionListener] = (),
    ) -> None:
        super().__init__(store, clock, ids, listeners)
        self._fraud = fraud_chain

    @staticmethod
    def fee_for(amount: Money) -> Money:
        """Integer basis points and floor division: the platform never rounds in its own favour."""
        return Money(amount.cents * MERCHANT_FEE_BASIS_POINTS // 10_000, amount.currency)

    def pay_merchant(self, idempotency_key: str, wallet_id: str, merchant_id: str, amount: Money) -> Transaction:
        """Fraud chain first, then one transaction that debits the wallet and splits the fee."""
        claimed = self._store.idempotency.claim(
            idempotency_key, f"pay:{wallet_id}:{merchant_id}:{amount.cents}"
        )
        if claimed is not None:
            return self._replay(claimed)
        destination = merchant_account(merchant_id)
        try:
            transaction = self._new(
                TransactionType.MERCHANT_PAYMENT, amount, wallet_account(wallet_id), destination, idempotency_key
            )
            decision = self._fraud.evaluate(
                FraudContext(wallet_id, amount, destination, self._clock.now(), tuple(self._store.history(wallet_id)))
            )
            with self._store.locked(wallet_id), PaymentUnitOfWork(self._store, [wallet_id]) as uow:
                uow.track(transaction)
                if not decision.allowed:
                    transaction.failure_reason = f"fraud:{decision.rule}:{decision.reason}"
                    transaction.transition_to(TransactionStatus.FAILED)
                else:
                    fee = self.fee_for(amount)
                    uow.wallet(wallet_id).debit(amount)  # the negative-balance guard lives here
                    transaction.transition_to(TransactionStatus.CAPTURED)
                    uow.post(
                        self._debit(transaction.id, wallet_account(wallet_id), amount),
                        self._credit(transaction.id, destination, amount - fee),
                        self._credit(transaction.id, FEES, fee),
                    )
                uow.commit()
        except Exception:
            self._store.idempotency.release(idempotency_key)
            raise
        return self._finish(idempotency_key, transaction.id, FraudRejectedError)

    def refund(self, idempotency_key: str, transaction_id: str, amount: Money | None = None) -> Refund:
        """Full or partial. The fee is reversed pro rata so the books stay symmetrical."""
        original = self._store.transaction(transaction_id)
        refund_amount = amount or original.refundable()
        claimed = self._store.idempotency.claim(
            idempotency_key, f"refund:{transaction_id}:{refund_amount.cents}"
        )
        if claimed is not None:
            return self._store.refund_for_key(idempotency_key)
        try:
            if refund_amount.cents <= 0 or refund_amount > original.refundable():
                raise ValidationError(
                    f"{original.id} can refund at most {original.refundable()}, asked for {refund_amount}"
                )
            wallet_id = wallet_id_of(original.source)
            refund = Refund(self._ids.next_id(), original.id, refund_amount, idempotency_key, self._clock.now())
            with self._store.locked(wallet_id), PaymentUnitOfWork(self._store, [wallet_id], [original.id]) as uow:
                current = uow.transaction(original.id)
                # Reverse the *incremental* fee, not the fee on this slice alone: floor
                # division on each slice would otherwise strand a cent in FEES when a
                # payment is refunded in parts. Full refunds always return exactly what
                # was charged, whatever the split.
                fee = self.fee_for(current.refunded + refund_amount) - self.fee_for(current.refunded)
                uow.wallet(wallet_id).credit(refund_amount)
                current.refunded = current.refunded + refund_amount
                current.transition_to(
                    TransactionStatus.REFUNDED
                    if current.refunded == current.amount
                    else TransactionStatus.PARTIALLY_REFUNDED
                )
                uow.refunds.append(refund)
                uow.post(
                    self._debit(current.id, current.destination, refund_amount - fee),
                    self._debit(current.id, FEES, fee),
                    self._credit(current.id, wallet_account(wallet_id), refund_amount),
                )
                uow.commit()
            self._store.idempotency.complete(idempotency_key, original.id)
        except Exception:
            self._store.idempotency.release(idempotency_key)
            raise
        self._notify(self._store.transaction(original.id))
        return refund


# --8<-- [end:payment_service]
