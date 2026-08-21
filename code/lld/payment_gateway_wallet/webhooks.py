"""Inbound processor callbacks: duplicated, reordered, and sometimes early.

Three defences, in this order:

1. **Duplicate delivery** -- ``event_id`` is remembered, so the same event twice
   is a no-op.
2. **Out-of-order delivery** -- an event whose ``STATUS_RANK`` is not higher than
   the transaction's current rank is ignored, so ``authorized`` landing after
   ``captured`` cannot walk the transaction backwards.
3. **Early delivery** -- an event for a reference we have not committed yet is
   parked and replayed by ``replay`` once the transaction row exists.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.payment_gateway_wallet.ledger import (
    PSP_CLEARING,
    LedgerEntry,
    wallet_account,
    wallet_id_of,
)
from lld.payment_gateway_wallet.models import (
    STATUS_RANK,
    EntryDirection,
    Transaction,
    TransactionStatus,
    TransactionType,
    WebhookEvent,
)
from lld.payment_gateway_wallet.services import TransactionListener
from lld.payment_gateway_wallet.store import PaymentStore, PaymentUnitOfWork


# --8<-- [start:webhooks]
class WebhookHandler:
    """Applies processor events to transactions, idempotently and in rank order."""

    def __init__(
        self,
        store: PaymentStore,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        listeners: Iterable[TransactionListener] = (),
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("L")
        self._listeners = list(listeners)
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._parked: dict[str, list[WebhookEvent]] = {}

    def handle(self, event: WebhookEvent) -> str:
        """Returns applied, ignored, duplicate or deferred -- the four honest outcomes."""
        transaction = self._store.transaction_for_reference(event.psp_reference)
        if transaction is None:
            with self._lock:
                if event.event_id in self._seen:
                    return "duplicate"
                self._parked.setdefault(event.psp_reference, []).append(event)
            return "deferred"
        with self._lock:
            if event.event_id in self._seen:
                return "duplicate"
            self._seen.add(event.event_id)
        if STATUS_RANK[event.status] <= STATUS_RANK[transaction.status]:
            return "ignored"  # a late authorization after a capture, for example
        self._apply(transaction, event)
        return "applied"

    def replay(self, psp_reference: str) -> list[str]:
        """Drain the events that arrived before we had committed the transaction row."""
        with self._lock:
            events = self._parked.pop(psp_reference, [])
        return [self.handle(event) for event in sorted(events, key=lambda e: STATUS_RANK[e.status])]

    def parked(self) -> int:
        with self._lock:
            return sum(len(events) for events in self._parked.values())

    def _apply(self, transaction: Transaction, event: WebhookEvent) -> None:
        wallet_ids = [wallet_id_of(a) for a in (transaction.source, transaction.destination) if a.startswith("wallet:")]
        with self._store.locked(*wallet_ids), PaymentUnitOfWork(
            self._store, wallet_ids, [transaction.id]
        ) as uow:
            current = uow.transaction(transaction.id)
            if STATUS_RANK[event.status] <= STATUS_RANK[current.status]:
                return  # someone else applied it while we were taking the lock
            if event.status is TransactionStatus.CAPTURED and current.type is TransactionType.TOP_UP:
                wallet_id = wallet_id_of(current.destination)
                uow.wallet(wallet_id).credit(current.amount)
                uow.post(
                    LedgerEntry(self._ids.next_id(), current.id, PSP_CLEARING, EntryDirection.DEBIT, current.amount, event.at),
                    LedgerEntry(self._ids.next_id(), current.id, wallet_account(wallet_id), EntryDirection.CREDIT, current.amount, event.at),
                )
            current.transition_to(event.status)
            uow.commit()
        settled = self._store.transaction(transaction.id)
        for listener in self._listeners:
            listener.on_transaction(settled)


# --8<-- [end:webhooks]
