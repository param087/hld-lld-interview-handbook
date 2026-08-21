"""The store, the idempotency store, the lock ordering and the Unit of Work.

Two rules keep this file honest:

1. Multi-wallet operations acquire locks **sorted by wallet id**. A transfer
   A -> B and a simultaneous B -> A therefore queue instead of deadlocking.
2. Nothing is written until ``commit``, and ``commit`` validates the ledger
   posting before it touches a single balance.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

from lld.payment_gateway_wallet.ledger import Ledger, LedgerEntry
from lld.payment_gateway_wallet.models import (
    IdempotencyConflictError,
    Refund,
    Transaction,
    UnknownEntityError,
    Wallet,
)


# --8<-- [start:idempotency]
@dataclass(slots=True)
class IdempotencyRecord:
    """One client request. ``transaction_id`` is None while the request is in flight."""

    key: str
    fingerprint: str
    transaction_id: str | None = None


class IdempotencyStore:
    """Claim, complete, release. The three verbs that make money movement effectively-once.

    ``claim`` returns the finished record for a replay, raises for a key reused
    with a different payload or still running, and otherwise reserves the key.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, IdempotencyRecord] = {}

    def claim(self, key: str, fingerprint: str) -> IdempotencyRecord | None:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                self._records[key] = IdempotencyRecord(key, fingerprint)
                return None
            if record.fingerprint != fingerprint:
                raise IdempotencyConflictError(f"key {key} was already used for a different request")
            if record.transaction_id is None:
                raise IdempotencyConflictError(f"key {key} is still in flight")
            return record

    def complete(self, key: str, transaction_id: str) -> None:
        with self._lock:
            self._records[key].transaction_id = transaction_id

    def release(self, key: str) -> None:
        """Only for unexpected failures: a business decline keeps its stored result."""
        with self._lock:
            record = self._records.get(key)
            if record is not None and record.transaction_id is None:
                del self._records[key]


# --8<-- [end:idempotency]


# --8<-- [start:store]
class PaymentStore:
    """Wallets, transactions, refunds, the ledger and one reentrant lock per wallet."""

    def __init__(self, currency: str = "USD") -> None:
        self._registry_lock = threading.Lock()
        self._wallets: dict[str, Wallet] = {}
        self._wallet_locks: dict[str, threading.RLock] = {}
        self._transactions: dict[str, Transaction] = {}
        self._by_reference: dict[str, str] = {}  # psp reference -> transaction id
        self._refunds: list[Refund] = []
        self.ledger = Ledger(currency)
        self.idempotency = IdempotencyStore()

    def open_wallet(self, wallet: Wallet) -> Wallet:
        with self._registry_lock:
            self._wallets[wallet.id] = wallet
            self._wallet_locks[wallet.id] = threading.RLock()
        return wallet

    def wallet(self, wallet_id: str) -> Wallet:
        """A private copy: callers mutate it inside a Unit of Work, never in place."""
        with self._registry_lock:
            try:
                return self._wallets[wallet_id].copy()
            except KeyError:
                raise UnknownEntityError(f"unknown wallet {wallet_id}") from None

    def transaction(self, transaction_id: str) -> Transaction:
        with self._registry_lock:
            try:
                return self._transactions[transaction_id].copy()
            except KeyError:
                raise UnknownEntityError(f"unknown transaction {transaction_id}") from None

    def transaction_for_reference(self, psp_reference: str) -> Transaction | None:
        with self._registry_lock:
            transaction_id = self._by_reference.get(psp_reference)
            return self._transactions[transaction_id].copy() if transaction_id else None

    def history(self, wallet_id: str) -> list[Transaction]:
        account = f"wallet:{wallet_id}"
        with self._registry_lock:
            return [
                t.copy()
                for t in self._transactions.values()
                if account in (t.source, t.destination)
            ]

    def refunds(self) -> list[Refund]:
        with self._registry_lock:
            return list(self._refunds)

    def refund_for_key(self, idempotency_key: str) -> Refund:
        with self._registry_lock:
            for refund in self._refunds:
                if refund.idempotency_key == idempotency_key:
                    return refund
        raise UnknownEntityError(f"no refund recorded for key {idempotency_key}")

    @contextmanager
    def locked(self, *wallet_ids: str) -> Iterator[None]:
        """Acquire every wallet lock in id order. Fixed order, no deadlock, no exceptions."""
        with self._registry_lock:
            missing = [w for w in wallet_ids if w not in self._wallet_locks]
            if missing:
                raise UnknownEntityError(f"unknown wallet {missing[0]}")
            locks = [self._wallet_locks[w] for w in sorted(set(wallet_ids))]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    def apply(
        self,
        wallets: Sequence[Wallet],
        transactions: Sequence[Transaction],
        entries: Sequence[LedgerEntry],
        refunds: Sequence[Refund],
    ) -> None:
        """Validate first, then write. Nothing here can fail once the check passes."""
        if entries:
            Ledger.check_balanced(entries)
        with self._registry_lock:
            for wallet in wallets:
                self._wallets[wallet.id] = wallet
            for transaction in transactions:
                self._transactions[transaction.id] = transaction
                if transaction.psp_reference:
                    self._by_reference[transaction.psp_reference] = transaction.id
            self._refunds.extend(refunds)
        if entries:
            self.ledger.post(entries)


# --8<-- [end:store]


# --8<-- [start:uow]
class UnitOfWork(Protocol):
    """Balances, transaction rows and ledger entries become visible together."""

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class PaymentUnitOfWork:
    """Working copies of the wallets and transactions one operation touches.

    ``commit`` hands the whole batch to ``PaymentStore.apply``, which checks the
    ledger posting *before* it writes anything. A transfer that would leave the
    books out by a cent therefore never moves a balance at all.
    """

    def __init__(
        self,
        store: PaymentStore,
        wallet_ids: Sequence[str] = (),
        transaction_ids: Sequence[str] = (),
    ) -> None:
        self._store = store
        self._committed = False
        self.wallets: dict[str, Wallet] = {w: store.wallet(w) for w in sorted(set(wallet_ids))}
        self.transactions: dict[str, Transaction] = {
            t: store.transaction(t) for t in sorted(set(transaction_ids))
        }
        self.entries: list[LedgerEntry] = []
        self.refunds: list[Refund] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._committed:
            self.rollback()

    def wallet(self, wallet_id: str) -> Wallet:
        try:
            return self.wallets[wallet_id]
        except KeyError:
            raise UnknownEntityError(f"wallet {wallet_id} was not opened in this transaction") from None

    def transaction(self, transaction_id: str) -> Transaction:
        try:
            return self.transactions[transaction_id]
        except KeyError:
            raise UnknownEntityError(f"transaction {transaction_id} is not in this transaction") from None

    def track(self, transaction: Transaction) -> Transaction:
        self.transactions[transaction.id] = transaction
        return transaction

    def post(self, *entries: LedgerEntry) -> None:
        self.entries.extend(entries)

    def commit(self) -> None:
        self._store.apply(
            list(self.wallets.values()), list(self.transactions.values()), self.entries, self.refunds
        )
        self._committed = True

    def rollback(self) -> None:
        self.entries.clear()
        self.refunds.clear()
        self.wallets = {w: self._store.wallet(w) for w in self.wallets}


# --8<-- [end:uow]
