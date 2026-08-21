"""One lock per barcoded copy: the only place ``BookItem.status`` ever changes.

Multi-copy operations acquire the locks in sorted barcode order, so two members
scanning overlapping stacks can never deadlock.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date

from lld.library_management.catalog import SearchableCatalog
from lld.library_management.models import (
    BookItem,
    ItemStateError,
    ItemStatus,
    ItemUnavailableError,
)


# --8<-- [start:item_locks]
class ItemLockService:
    """The scarce-inventory gatekeeper: one lock per barcoded copy, sorted acquisition.

    Every transition of ``BookItem.status`` in the whole system happens in here.
    """

    def __init__(self, catalog: SearchableCatalog) -> None:
        self._catalog = catalog
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def _lock_for(self, barcode: str) -> threading.Lock:
        with self._registry_lock:
            return self._locks.setdefault(barcode, threading.Lock())

    @contextmanager
    def items_locked(self, barcodes: Sequence[str]) -> Iterator[None]:
        """Sorted order: a member taking C-002 and C-001 always locks C-001 first."""
        acquired: list[threading.Lock] = []
        try:
            for barcode in sorted(set(barcodes)):
                lock = self._lock_for(barcode)
                lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def lend(self, barcodes: Sequence[str], account_id: str, due_on: date) -> list[BookItem]:
        """All-or-nothing: check every copy, then lend every copy."""
        with self.items_locked(barcodes):
            items = [self._catalog.item(b) for b in barcodes]
            blocked = sorted(i.barcode for i in items if not i.is_borrowable_by(account_id))
            if blocked:
                raise ItemUnavailableError(f"copies not available: {', '.join(blocked)}")
            for item in items:
                item.lend_to(account_id, due_on)
            return items

    def take_back(self, barcode: str, next_holder: str | None, pickup_by: date | None) -> BookItem:
        """LOANED to RESERVED (somebody is waiting) or to AVAILABLE (nobody is)."""
        with self.items_locked([barcode]):
            item = self._catalog.item(barcode)
            if item.status is not ItemStatus.LOANED:
                raise ItemStateError(f"copy {barcode} is {item.status}, not on loan")
            if next_holder is None:
                item.shelve()
            else:
                item.put_on_hold_shelf(next_holder)
                item.due_on = pickup_by
            return item

    def reserve_for(self, barcode: str, account_id: str) -> bool:
        """Put a free copy on the hold shelf. False if somebody borrowed it first."""
        with self.items_locked([barcode]):
            item = self._catalog.item(barcode)
            if item.status is not ItemStatus.AVAILABLE:
                return False
            item.put_on_hold_shelf(account_id)
            return True

    def release_hold_shelf(self, barcode: str) -> bool:
        with self.items_locked([barcode]):
            item = self._catalog.item(barcode)
            if item.status is not ItemStatus.RESERVED:
                return False
            item.shelve()
            return True

    def mark(self, barcode: str, status: ItemStatus) -> BookItem:
        with self.items_locked([barcode]):
            item = self._catalog.item(barcode)
            item.mark(status)
            return item

    def repair(self, barcode: str) -> BookItem:
        with self.items_locked([barcode]):
            item = self._catalog.item(barcode)
            if item.status not in (ItemStatus.DAMAGED, ItemStatus.LOST):
                raise ItemStateError(f"copy {barcode} is {item.status}, nothing to repair")
            item.shelve()
            return item

    def set_due(self, barcode: str, due_on: date) -> None:
        with self.items_locked([barcode]):
            self._catalog.item(barcode).due_on = due_on

    def first_available(self, book_id: str) -> BookItem | None:
        """Lock-free hint. Every caller re-checks under the copy lock."""
        for item in self._catalog.copies(book_id):
            if item.status is ItemStatus.AVAILABLE:
                return item
        return None


# --8<-- [end:item_locks]
