"""The circulation desk: checkout, return, renewal, holds and fines.

Two lock families, the same shape as the movie-ticket and hotel siblings:

* ``ItemLockService._locks`` holds **one lock per barcode**. It guards copy status, the
  borrower and the hold shelf. A multi-copy checkout takes the locks in sorted barcode
  order, so two members grabbing overlapping stacks can never deadlock.
* ``LibraryService._ledger_lock`` guards accounts, loans, fines, reservations and the
  hold queues. It is never held while a copy lock is held; promoting a hold therefore
  happens *before* the copy is touched, and is rolled back if the copy has gone.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import date, timedelta

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock
from lld.library_management.catalog import SearchableCatalog
from lld.library_management.locks import ItemLockService
from lld.library_management.models import (
    LOAN_PERIOD_DAYS,
    PICKUP_WINDOW_DAYS,
    Account,
    AccountBlockedError,
    AccountFactory,
    AccountStatus,
    Book,
    BookItem,
    Fine,
    ItemStateError,
    ItemStatus,
    ItemUnavailableError,
    Loan,
    LoanLimitError,
    LoanStatus,
    NotInCatalogError,
    Person,
    RenewalBlockedError,
    Reservation,
    ReservationStatus,
)
from lld.library_management.ports import HoldListener
from lld.library_management.strategies import DEFAULT_LOST_ITEM_FEE, FinePolicy, PerDayFine


# --8<-- [start:library_service]
class LibraryService:
    """Facade for the circulation desk. Everything the librarian's screen calls."""

    BLOCK_THRESHOLD = Money.of("10.00")

    def __init__(
        self,
        catalog: SearchableCatalog,
        items: ItemLockService,
        fines: FinePolicy | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._catalog = catalog
        self._items = items
        self._fines = fines or PerDayFine()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("LN")
        self._accounts: dict[str, Account] = {}
        self._loans: dict[str, Loan] = {}
        self._fine_ledger: dict[str, Fine] = {}
        self._reservations: dict[str, Reservation] = {}
        self._queues: dict[str, list[Reservation]] = {}
        self._listeners: list[HoldListener] = []
        self._ledger_lock = threading.Lock()

    def subscribe(self, listener: HoldListener) -> None:
        self._listeners.append(listener)

    def today(self) -> date:
        return self._clock.now_dt().date()

    def register(self, person: Person) -> Account:
        account = AccountFactory.open(self._ids.next_id(), person)
        with self._ledger_lock:
            self._accounts[account.id] = account
        return account

    def search(self, query: str) -> list[Book]:
        return self._catalog.search(query)

    # -- circulation -------------------------------------------------------------------
    def checkout(self, account_id: str, barcodes: Sequence[str]) -> list[Loan]:
        """Reserve the quota slots, take the copies, then commit the loans.

        The quota is claimed under the ledger lock *before* any copy lock, so two
        concurrent checkouts can never push one account past its limit.
        """
        wanted = tuple(sorted(set(barcodes)))
        if not wanted:
            raise ItemUnavailableError("scan at least one barcode")
        today = self.today()
        due_on = today + timedelta(days=LOAN_PERIOD_DAYS)
        with self._ledger_lock:
            account = self._require_account(account_id)
            self._check_borrowable(account)
            if len(account.borrowed) + len(wanted) > account.max_loans:
                raise LoanLimitError(
                    f"account {account_id} may hold {account.max_loans} items; "
                    f"has {len(account.borrowed)}, asked for {len(wanted)}"
                )
            account.borrowed.update(wanted)
        try:
            self._items.lend(wanted, account_id, due_on)
        except (ItemUnavailableError, NotInCatalogError):
            with self._ledger_lock:
                account.borrowed.difference_update(wanted)
            raise
        loans = []
        with self._ledger_lock:
            for barcode in wanted:
                item = self._catalog.item(barcode)
                loan = Loan(
                    id=self._ids.next_id(),
                    barcode=barcode,
                    book_id=item.book_id,
                    account_id=account_id,
                    borrowed_on=today,
                    due_on=due_on,
                )
                self._loans[loan.id] = loan
                loans.append(loan)
                self._fulfil_ready_hold(account_id, barcode)
        return loans

    def return_item(self, barcode: str) -> Fine | None:
        """Close the loan, price the delay, then hand the copy to the next hold."""
        today = self.today()
        with self._ledger_lock:
            loan = self._active_loan(barcode)
            loan.returned_on = today
            loan.status = LoanStatus.RETURNED
            account = self._accounts[loan.account_id]
            account.borrowed.discard(barcode)
            fine = self._assess(account, loan, today, self._fines.fine_for(loan, today), "overdue")
            reservation = self._promote(loan.book_id, barcode, today)
        next_holder = reservation.account_id if reservation is not None else None
        pickup_by = reservation.pickup_by if reservation is not None else None
        self._items.take_back(barcode, next_holder, pickup_by)
        if reservation is not None:
            self._emit("hold_ready", reservation)
        return fine

    def renew(self, account_id: str, barcode: str) -> date:
        """Refused when somebody is waiting: a queue that renewals can jump is not a queue."""
        today = self.today()
        with self._ledger_lock:
            loan = self._active_loan(barcode)
            if loan.account_id != account_id:
                raise RenewalBlockedError(f"copy {barcode} is not on {account_id}'s card")
            self._check_borrowable(self._require_account(account_id))
            if any(r.status is ReservationStatus.WAITING for r in self._queues.get(loan.book_id, [])):
                raise RenewalBlockedError(f"another member is waiting for {loan.book_id}")
            due_on = loan.renew(today, LOAN_PERIOD_DAYS)
        self._items.set_due(barcode, due_on)
        return due_on

    # -- holds -------------------------------------------------------------------------
    def place_hold(self, account_id: str, book_id: str) -> Reservation:
        today = self.today()
        with self._ledger_lock:
            self._check_borrowable(self._require_account(account_id))
            queue = self._queues.setdefault(book_id, [])
            if any(
                r.account_id == account_id
                and r.status in (ReservationStatus.WAITING, ReservationStatus.READY)
                for r in queue
            ):
                raise ItemUnavailableError(f"{account_id} already holds a place for {book_id}")
            reservation = Reservation(
                id=self._ids.next_id(),
                book_id=book_id,
                account_id=account_id,
                placed_at=self._clock.now(),
            )
            queue.append(reservation)
            self._reservations[reservation.id] = reservation
        free = self._items.first_available(book_id)
        if free is not None:
            self._offer(book_id, free.barcode, today)
        return reservation

    def cancel_hold(self, reservation_id: str) -> None:
        with self._ledger_lock:
            reservation = self._require_reservation(reservation_id)
            if reservation.status in (ReservationStatus.FULFILLED, ReservationStatus.CANCELLED):
                raise ItemStateError(f"reservation {reservation_id} is {reservation.status}")
            was_ready, barcode = reservation.status is ReservationStatus.READY, reservation.barcode
            reservation.status = ReservationStatus.CANCELLED
        if was_ready and barcode is not None:
            self._recycle(reservation.book_id, barcode)

    def expire_holds(self) -> list[str]:
        """Pickup window closed: shelve the copy or pass it to the next member."""
        today = self.today()
        lapsed: list[Reservation] = []
        with self._ledger_lock:
            for reservation in self._reservations.values():
                if (
                    reservation.status is ReservationStatus.READY
                    and reservation.pickup_by is not None
                    and reservation.pickup_by < today
                ):
                    reservation.status = ReservationStatus.EXPIRED
                    lapsed.append(reservation)
        for reservation in lapsed:
            self._emit("hold_expired", reservation)
            if reservation.barcode is not None:
                self._recycle(reservation.book_id, reservation.barcode)
        return [r.id for r in lapsed]

    # -- money and condition -----------------------------------------------------------
    def pay_fine(self, fine_id: str) -> Account:
        with self._ledger_lock:
            fine = self._fine_ledger.get(fine_id)
            if fine is None:
                raise NotInCatalogError(f"unknown fine {fine_id!r}")
            fine.paid = True
            account = self._accounts[fine.account_id]
            if (
                account.status is AccountStatus.BLOCKED
                and account.unpaid_total() < self.BLOCK_THRESHOLD
            ):
                account.status = AccountStatus.ACTIVE
            return account

    def mark_lost(self, barcode: str) -> Fine | None:
        today = self.today()
        with self._ledger_lock:
            loan = self._active_loan(barcode)
            loan.status = LoanStatus.LOST
            loan.returned_on = today
            account = self._accounts[loan.account_id]
            account.borrowed.discard(barcode)
            fine = self._assess(account, loan, today, DEFAULT_LOST_ITEM_FEE, "lost item")
        self._items.mark(barcode, ItemStatus.LOST)
        return fine

    def mark_damaged(self, barcode: str) -> BookItem:
        return self._items.mark(barcode, ItemStatus.DAMAGED)

    def account(self, account_id: str) -> Account:
        with self._ledger_lock:
            return self._require_account(account_id)

    def queue_for(self, book_id: str) -> list[Reservation]:
        with self._ledger_lock:
            return [r for r in self._queues.get(book_id, []) if r.status is ReservationStatus.WAITING]

    # -- internals ---------------------------------------------------------------------
    def _offer(self, book_id: str, barcode: str, today: date) -> Reservation | None:
        """Promote under the ledger lock, then claim the copy; demote if it has gone."""
        with self._ledger_lock:
            reservation = self._promote(book_id, barcode, today)
        if reservation is None:
            return None
        if self._items.reserve_for(barcode, reservation.account_id):
            self._emit("hold_ready", reservation)
            return reservation
        with self._ledger_lock:  # somebody borrowed it first: back to the head of the queue
            reservation.status = ReservationStatus.WAITING
            reservation.barcode = None
            reservation.pickup_by = None
        return None

    def _recycle(self, book_id: str, barcode: str) -> None:
        if self._items.release_hold_shelf(barcode):
            self._offer(book_id, barcode, self.today())

    def _promote(self, book_id: str, barcode: str, today: date) -> Reservation | None:
        """Ledger lock held. FIFO: the earliest waiting hold gets the copy."""
        for reservation in self._queues.get(book_id, []):
            if reservation.status is ReservationStatus.WAITING:
                reservation.status = ReservationStatus.READY
                reservation.barcode = barcode
                reservation.pickup_by = today + timedelta(days=PICKUP_WINDOW_DAYS)
                return reservation
        return None

    def _fulfil_ready_hold(self, account_id: str, barcode: str) -> None:
        for reservation in self._reservations.values():
            if (
                reservation.status is ReservationStatus.READY
                and reservation.barcode == barcode
                and reservation.account_id == account_id
            ):
                reservation.status = ReservationStatus.FULFILLED

    def _assess(
        self, account: Account, loan: Loan, today: date, amount: Money, reason: str
    ) -> Fine | None:
        if amount.is_zero():
            return None
        fine = Fine(
            id=self._ids.next_id(),
            account_id=account.id,
            barcode=loan.barcode,
            amount=amount,
            reason=reason,
            assessed_on=today,
        )
        account.fines.append(fine)
        self._fine_ledger[fine.id] = fine
        if account.unpaid_total() >= self.BLOCK_THRESHOLD:
            account.status = AccountStatus.BLOCKED
        return fine

    def _check_borrowable(self, account: Account) -> None:
        if account.status is not AccountStatus.ACTIVE:
            raise AccountBlockedError(
                f"account {account.id} is {account.status} (owes {account.unpaid_total()})"
            )

    def _active_loan(self, barcode: str) -> Loan:
        for loan in self._loans.values():
            if loan.barcode == barcode and loan.status is LoanStatus.ACTIVE:
                return loan
        raise NotInCatalogError(f"no active loan for copy {barcode!r}")

    def _require_account(self, account_id: str) -> Account:
        try:
            return self._accounts[account_id]
        except KeyError:
            raise NotInCatalogError(f"unknown account {account_id!r}") from None

    def _require_reservation(self, reservation_id: str) -> Reservation:
        try:
            return self._reservations[reservation_id]
        except KeyError:
            raise NotInCatalogError(f"unknown reservation {reservation_id!r}") from None

    def _emit(self, event: str, reservation: Reservation) -> None:
        # Outside the lock: a slow notifier must never stall the returns desk.
        book = self._catalog.book(reservation.book_id)
        for listener in self._listeners:
            listener.on_hold_event(event, reservation, book)


# --8<-- [end:library_service]
