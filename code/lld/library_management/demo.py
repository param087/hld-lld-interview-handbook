"""A week at the circulation desk: search, borrow, hold, return, fine, unblock."""

from common import FakeClock, SequentialIdGenerator
from lld.library_management.catalog import CachingCatalog, Catalog
from lld.library_management.locks import ItemLockService
from lld.library_management.models import (
    AccountBlockedError,
    Author,
    Book,
    ItemUnavailableError,
    Member,
    RenewalBlockedError,
)
from lld.library_management.ports import NotificationService
from lld.library_management.services import LibraryService
from lld.library_management.strategies import PerDayFine

DAY = 86_400.0
START = 1_773_216_000.0  # 2026-03-11 08:00 UTC


def build_catalog() -> CachingCatalog:
    inner = Catalog()
    catalog = CachingCatalog(inner)
    gaiman = Author("a-1", "Neil Gaiman")
    hofstadter = Author("a-2", "Douglas Hofstadter")
    catalog.add_book(Book("b-1", "9780062255655", "American Gods", (gaiman,), ("fantasy",)))
    catalog.add_book(Book("b-2", "9780465026562", "Godel Escher Bach", (hofstadter,), ("logic",)))
    catalog.add_copies("b-1", ["C-001", "C-002"])
    catalog.add_copies("b-2", ["C-003"])
    return catalog


def main() -> None:
    clock = FakeClock(start=START)
    catalog = build_catalog()
    items = ItemLockService(catalog)
    notifier = NotificationService()
    library = LibraryService(
        catalog, items, fines=PerDayFine(), clock=clock, ids=SequentialIdGenerator("LN")
    )
    library.subscribe(notifier)

    asha = library.register(Member("p-1", "Asha", "asha@example.com"))
    bala = library.register(Member("p-2", "Bala", "bala@example.com"))
    print(f"accounts: {asha.id} (limit {asha.max_loans}), {bala.id} (limit {bala.max_loans})")
    print(f"search 'gaiman' -> {[b.title for b in library.search('gaiman')]}")
    library.search("gaiman")
    print(f"caching proxy: {catalog.hits} hit, {catalog.misses} miss")

    loans = library.checkout(asha.id, ["C-001", "C-003"])
    print(f"{asha.id} borrows {[loan.barcode for loan in loans]}, due {loans[0].due_on}")
    try:
        library.checkout(bala.id, ["C-003"])
    except ItemUnavailableError as exc:
        print(f"last copy contended: {exc}")

    hold = library.place_hold(bala.id, "b-2")
    print(f"{bala.id} joins the queue for Godel Escher Bach -> {hold.status}")
    try:
        library.renew(asha.id, "C-003")
    except RenewalBlockedError as exc:
        print(f"renewal refused: {exc}")

    clock.advance(50 * DAY)  # forty days past the 10-day due date
    fine = library.return_item("C-003")
    print(f"returned 40 days late -> fine {fine.amount} ({fine.reason}, capped), account now {library.account(asha.id).status}")
    print(f"notified: {notifier.outbox()[-1]}")
    try:
        library.checkout(asha.id, ["C-002"])
    except AccountBlockedError as exc:
        print(f"borrowing blocked: {exc}")

    library.pay_fine(fine.id)
    print(f"fine paid -> {library.account(asha.id).status}, can borrow again")
    library.checkout(bala.id, ["C-003"])
    print(f"{bala.id} collects the hold: {hold.status}, queue for b-2 is now {library.queue_for('b-2')}")
    print(f"C-001 marked lost -> fee {library.mark_lost('C-001').amount}, copy is {catalog.item('C-001').status}")


if __name__ == "__main__":
    main()
