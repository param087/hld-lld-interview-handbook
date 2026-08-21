"""Last-copy contention, the hold queue, renewal conflicts and fine arithmetic."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from common import ConflictError, FakeClock, Money, SequentialIdGenerator
from lld.library_management.catalog import CachingCatalog, Catalog
from lld.library_management.locks import ItemLockService
from lld.library_management.models import (
    AccountBlockedError,
    AccountStatus,
    Author,
    Book,
    ItemStatus,
    ItemUnavailableError,
    Librarian,
    Loan,
    LoanLimitError,
    LoanStatus,
    Member,
    NotInCatalogError,
    RenewalBlockedError,
    ReservationStatus,
)
from lld.library_management.ports import NotificationService
from lld.library_management.services import LibraryService
from lld.library_management.strategies import NoFine, PerDayFine, TieredFine

DAY = 86_400.0
START = 1_773_216_000.0  # 2026-03-11 08:00 UTC
DUE = date(2026, 3, 21)


def build(clock: FakeClock, copies: int = 2, **kwargs: object) -> tuple[Catalog, LibraryService]:
    catalog = Catalog()
    catalog.add_book(Book("b-1", "9780062255655", "American Gods", (Author("a-1", "Neil Gaiman"),), ("fantasy",)))
    catalog.add_book(Book("b-2", "9780465026562", "Godel Escher Bach", (Author("a-2", "Douglas Hofstadter"),), ("logic",)))
    catalog.add_copies("b-1", [f"C-{i:03d}" for i in range(1, copies + 1)])
    catalog.add_copies("b-2", ["C-900"])
    items = ItemLockService(catalog)
    service = LibraryService(catalog, items, clock=clock, ids=SequentialIdGenerator("LN"), **kwargs)
    return catalog, service


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


def test_checkout_and_return_moves_the_copy_through_its_states(clock: FakeClock) -> None:
    catalog, library = build(clock)
    account = library.register(Member("p-1", "Asha", "asha@example.com"))
    loans = library.checkout(account.id, ["C-001", "C-900"])
    assert [loan.barcode for loan in loans] == ["C-001", "C-900"]
    assert loans[0].due_on == DUE and loans[0].status is LoanStatus.ACTIVE
    assert catalog.item("C-001").status is ItemStatus.LOANED
    assert library.account(account.id).borrowed == {"C-001", "C-900"}

    assert library.return_item("C-001") is None  # returned on time, no fine
    assert catalog.item("C-001").status is ItemStatus.AVAILABLE
    assert library.account(account.id).borrowed == {"C-900"}
    with pytest.raises(NotInCatalogError):
        library.return_item("C-001")  # already back on the shelf


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("gaiman", ["American Gods"]),
        ("GODEL", ["Godel Escher Bach"]),
        ("9780062255655", ["American Gods"]),
        ("logic", ["Godel Escher Bach"]),
        ("nothing here", []),
    ],
)
def test_search_covers_title_author_isbn_and_subject(
    clock: FakeClock, query: str, expected: list[str]
) -> None:
    catalog, library = build(clock)
    assert [b.title for b in library.search(query)] == expected


def test_caching_proxy_serves_repeats_and_invalidates_on_new_stock(clock: FakeClock) -> None:
    inner = Catalog()
    proxy = CachingCatalog(inner)
    proxy.add_book(Book("b-1", "111", "American Gods", (Author("a-1", "Neil Gaiman"),)))
    assert [b.title for b in proxy.search("gaiman")] == ["American Gods"]
    proxy.search("gaiman")
    assert (proxy.hits, proxy.misses) == (1, 1)
    proxy.add_book(Book("b-9", "222", "Anansi Boys", (Author("a-1", "Neil Gaiman"),)))
    assert len(proxy.search("gaiman")) == 2  # the cache was invalidated, not stale
    assert proxy.misses == 2


def test_loan_limit_and_blocked_account_stop_borrowing(clock: FakeClock) -> None:
    catalog, library = build(clock, copies=8)
    account = library.register(Member("p-1", "Asha", "asha@example.com"))
    library.checkout(account.id, [f"C-{i:03d}" for i in range(1, 6)])  # five is the member limit
    with pytest.raises(LoanLimitError, match="may hold 5 items"):
        library.checkout(account.id, ["C-006"])
    assert catalog.item("C-006").status is ItemStatus.AVAILABLE  # the slot was rolled back

    staff = library.register(Librarian("p-2", "Ravi", "ravi@example.com"))
    assert library.account(staff.id).max_loans == 10
    library.checkout(staff.id, ["C-006", "C-007"])

    clock.advance(60 * DAY)
    fine = library.return_item("C-001")
    assert fine is not None and fine.amount == Money.of("10.00")  # capped
    assert library.account(account.id).status is AccountStatus.BLOCKED
    with pytest.raises(AccountBlockedError):
        library.checkout(account.id, ["C-008"])
    library.pay_fine(fine.id)
    assert library.account(account.id).status is AccountStatus.ACTIVE
    assert library.checkout(account.id, ["C-008"])[0].barcode == "C-008"


# --8<-- [start:holds]
def test_hold_queue_is_fifo_and_a_return_puts_the_copy_on_the_hold_shelf(clock: FakeClock) -> None:
    catalog, library = build(clock, copies=1)
    notifier = NotificationService()
    library.subscribe(notifier)
    asha = library.register(Member("p-1", "Asha", "asha@example.com"))
    bala = library.register(Member("p-2", "Bala", "bala@example.com"))
    chitra = library.register(Member("p-3", "Chitra", "chitra@example.com"))

    library.checkout(asha.id, ["C-001"])
    first = library.place_hold(bala.id, "b-1")
    second = library.place_hold(chitra.id, "b-1")
    assert [r.id for r in library.queue_for("b-1")] == [first.id, second.id]

    library.return_item("C-001")
    assert first.status is ReservationStatus.READY and first.barcode == "C-001"
    assert second.status is ReservationStatus.WAITING  # still queued behind Bala
    assert catalog.item("C-001").reserved_for == bala.id
    with pytest.raises(ItemUnavailableError):
        library.checkout(chitra.id, ["C-001"])  # it is on the hold shelf for Bala
    library.checkout(bala.id, ["C-001"])
    assert first.status is ReservationStatus.FULFILLED
    assert notifier.outbox()[0].startswith("hold_ready: American Gods")


# --8<-- [end:holds]


def test_expired_hold_passes_the_copy_to_the_next_member(clock: FakeClock) -> None:
    catalog, library = build(clock, copies=1)
    asha = library.register(Member("p-1", "Asha", "asha@example.com"))
    bala = library.register(Member("p-2", "Bala", "bala@example.com"))
    chitra = library.register(Member("p-3", "Chitra", "chitra@example.com"))
    library.checkout(asha.id, ["C-001"])
    first = library.place_hold(bala.id, "b-1")
    second = library.place_hold(chitra.id, "b-1")
    library.return_item("C-001")

    assert library.expire_holds() == []  # the pickup window is still open
    clock.advance(4 * DAY)
    assert library.expire_holds() == [first.id]
    assert first.status is ReservationStatus.EXPIRED
    assert second.status is ReservationStatus.READY  # passed straight down the queue
    assert catalog.item("C-001").reserved_for == chitra.id


def test_renewal_is_blocked_by_a_waiting_hold_and_by_the_cap(clock: FakeClock) -> None:
    catalog, library = build(clock, copies=1)
    asha = library.register(Member("p-1", "Asha", "asha@example.com"))
    bala = library.register(Member("p-2", "Bala", "bala@example.com"))
    library.checkout(asha.id, ["C-001"])
    assert library.renew(asha.id, "C-001") == date(2026, 3, 31)
    assert catalog.item("C-001").due_on == date(2026, 3, 31)
    assert library.renew(asha.id, "C-001") == date(2026, 4, 10)
    with pytest.raises(RenewalBlockedError, match="renewals"):
        library.renew(asha.id, "C-001")  # two renewals is the cap

    library.return_item("C-001")
    library.checkout(bala.id, ["C-001"])
    library.place_hold(asha.id, "b-1")
    with pytest.raises(RenewalBlockedError, match="waiting"):
        library.renew(bala.id, "C-001")


def test_cancelled_hold_releases_the_copy_to_the_next_in_line(clock: FakeClock) -> None:
    catalog, library = build(clock, copies=1)
    asha = library.register(Member("p-1", "Asha", "asha@example.com"))
    bala = library.register(Member("p-2", "Bala", "bala@example.com"))
    chitra = library.register(Member("p-3", "Chitra", "chitra@example.com"))
    library.checkout(asha.id, ["C-001"])
    first = library.place_hold(bala.id, "b-1")
    second = library.place_hold(chitra.id, "b-1")
    library.return_item("C-001")
    library.cancel_hold(first.id)
    assert first.status is ReservationStatus.CANCELLED
    assert second.status is ReservationStatus.READY
    assert catalog.item("C-001").reserved_for == chitra.id


def test_lost_and_damaged_copies_leave_circulation(clock: FakeClock) -> None:
    catalog, library = build(clock, copies=2)
    asha = library.register(Member("p-1", "Asha", "asha@example.com"))
    library.checkout(asha.id, ["C-001"])
    fine = library.mark_lost("C-001")
    assert fine is not None and fine.amount == Money.of("30.00")
    assert catalog.item("C-001").status is ItemStatus.LOST
    assert library.account(asha.id).borrowed == set()
    assert library.account(asha.id).status is AccountStatus.BLOCKED

    library.mark_damaged("C-002")
    assert catalog.item("C-002").status is ItemStatus.DAMAGED
    with pytest.raises(ItemUnavailableError):
        library.checkout(library.register(Member("p-2", "Bala", "b@example.com")).id, ["C-002"])


@pytest.mark.parametrize(
    ("policy", "days_late", "expected"),
    [
        (PerDayFine(), 0, "0.00"),
        (PerDayFine(), 4, "1.00"),
        (PerDayFine(grace_days=3), 4, "0.25"),
        (PerDayFine(), 100, "10.00"),  # the cap
        (TieredFine(), 7, "0.70"),
        (TieredFine(), 10, "2.20"),  # 7 x 0.10 + 3 x 0.50
        (NoFine(), 100, "0.00"),
    ],
)
def test_fine_policies(policy: object, days_late: int, expected: str) -> None:
    loan = Loan(
        id="LN-1",
        barcode="C-001",
        book_id="b-1",
        account_id="LN-0",
        borrowed_on=date(2026, 3, 11),
        due_on=DUE,
    )
    today = date.fromordinal(DUE.toordinal() + days_late)
    assert policy.fine_for(loan, today) == Money.of(expected)


# --8<-- [start:concurrency]
def test_thirty_members_race_for_the_last_copy_and_exactly_one_wins(clock: FakeClock) -> None:
    catalog, library = build(clock, copies=1)
    accounts = [
        library.register(Member(f"p-{i}", f"Member {i}", f"m{i}@example.com")) for i in range(30)
    ]

    def borrow(i: int) -> str | None:
        try:
            return library.checkout(accounts[i].id, ["C-001"])[0].account_id
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        winners = [w for w in pool.map(borrow, range(30)) if w is not None]

    assert len(winners) == 1  # one copy, one loan
    item = catalog.item("C-001")
    assert item.status is ItemStatus.LOANED and item.borrower_id == winners[0]
    assert sum(1 for a in accounts if library.account(a.id).borrowed) == 1


def test_the_loan_limit_holds_under_concurrent_checkouts(clock: FakeClock) -> None:
    _, library = build(clock, copies=12)
    account = library.register(Member("p-1", "Asha", "asha@example.com"))

    def borrow(i: int) -> bool:
        try:
            library.checkout(account.id, [f"C-{i:03d}"])
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(borrow, range(1, 13)))

    assert sum(results) == 5  # the member limit, not eleven
    assert len(library.account(account.id).borrowed) == 5


# --8<-- [end:concurrency]
