"""The double-entry ledger: the one place that decides whether the books are right.

Every posting is a list of entries whose debits equal its credits. ``post``
refuses anything else, so an imbalanced write cannot exist even transiently --
which is why the tests can assert ``is_balanced()`` after every operation.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass

from common import Money
from lld.payment_gateway_wallet.models import EntryDirection, LedgerImbalanceError

PSP_CLEARING = "psp_clearing"  # money in flight at the processor
FEES = "fees"  # what the platform keeps


def wallet_account(wallet_id: str) -> str:
    return f"wallet:{wallet_id}"


def merchant_account(merchant_id: str) -> str:
    return f"merchant:{merchant_id}"


def wallet_id_of(account: str) -> str:
    """Inverse of ``wallet_account``; raises if the account is not a wallet."""
    if not account.startswith("wallet:"):
        raise ValueError(f"{account} is not a wallet account")
    return account.removeprefix("wallet:")


# --8<-- [start:ledger]
@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One side of a posting. Frozen and append-only: the ledger is never edited."""

    id: str
    transaction_id: str
    account: str
    direction: EntryDirection
    amount: Money
    at: float

    def signed_cents(self) -> int:
        """Credit is positive, debit is negative, so a balanced posting sums to zero."""
        return self.amount.cents if self.direction is EntryDirection.CREDIT else -self.amount.cents


class Ledger:
    """Append-only book of entries, plus the balance of any account.

    ``_lock`` guards the entry list and the per-account totals. Reads are cheap
    because the totals are maintained on write rather than recomputed.
    """

    def __init__(self, currency: str = "USD") -> None:
        self._currency = currency
        self._lock = threading.Lock()
        self._entries: list[LedgerEntry] = []
        self._totals: dict[str, int] = {}

    @staticmethod
    def check_balanced(entries: Sequence[LedgerEntry]) -> None:
        """Debits must equal credits. This is the invariant the whole design protects."""
        if not entries:
            raise LedgerImbalanceError("a posting needs at least two entries")
        residual = sum(entry.signed_cents() for entry in entries)
        if residual != 0:
            raise LedgerImbalanceError(f"posting is out by {residual} cents; debits must equal credits")

    def post(self, entries: Sequence[LedgerEntry]) -> None:
        self.check_balanced(entries)
        with self._lock:
            for entry in entries:
                self._entries.append(entry)
                self._totals[entry.account] = self._totals.get(entry.account, 0) + entry.signed_cents()

    def balance(self, account: str) -> Money:
        """Credits minus debits: positive means the account holds money."""
        with self._lock:
            return Money(self._totals.get(account, 0), self._currency)

    def entries_for(self, transaction_id: str) -> list[LedgerEntry]:
        with self._lock:
            return [entry for entry in self._entries if entry.transaction_id == transaction_id]

    def is_balanced(self) -> bool:
        """The whole book: every cent debited somewhere was credited somewhere else."""
        with self._lock:
            return sum(self._totals.values()) == 0

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


# --8<-- [end:ledger]
