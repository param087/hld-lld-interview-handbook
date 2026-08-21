"""Catalog records, physical copies, accounts, loans, reservations and fines.

The distinction that carries this problem is ``Book`` versus ``BookItem``: a *Book* is
the catalog record (one ISBN, one title), a *BookItem* is a barcoded copy on a shelf.
You search books; you borrow items; holds queue on books and are satisfied by items.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError

LOAN_PERIOD_DAYS = 10
MAX_RENEWALS = 2
PICKUP_WINDOW_DAYS = 3


# --8<-- [start:enums]
class ItemStatus(StrEnum):
    AVAILABLE = "available"
    RESERVED = "reserved"  # waiting on the hold shelf for one named member
    LOANED = "loaned"
    LOST = "lost"
    DAMAGED = "damaged"


class LoanStatus(StrEnum):
    ACTIVE = "active"
    RETURNED = "returned"
    LOST = "lost"


class ReservationStatus(StrEnum):
    WAITING = "waiting"  # in the queue for the next free copy
    READY = "ready"  # a copy is on the hold shelf
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"  # the pickup window closed


class AccountStatus(StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"  # unpaid fines over the threshold
    CLOSED = "closed"


class Role(StrEnum):
    MEMBER = "member"
    LIBRARIAN = "librarian"


# --8<-- [end:enums]


# --8<-- [start:errors]
class ItemUnavailableError(ConflictError):
    """The copy is on loan, on the hold shelf for somebody else, lost or damaged."""


class LoanLimitError(ConflictError):
    """The account is already holding its maximum number of items."""


class AccountBlockedError(ConflictError):
    """Unpaid fines are over the threshold; settle up before borrowing again."""


class RenewalBlockedError(ConflictError):
    """Somebody is waiting for this title, or the renewal cap is reached."""


class ItemStateError(InvalidStateError):
    """The copy is not in a state that allows this transition."""


class NotInCatalogError(NotFoundError):
    """Unknown book, barcode, account or reservation id."""


# --8<-- [end:errors]


# --8<-- [start:catalog_models]
@dataclass(frozen=True, slots=True)
class Author:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Book:
    """The catalog record. Immutable: a new edition is a new Book, not an edit."""

    id: str
    isbn: str
    title: str
    authors: tuple[Author, ...] = ()
    subjects: tuple[str, ...] = ()

    def matches(self, needle: str) -> bool:
        """Title, author name, ISBN or subject - one predicate, used by every search."""
        needle = needle.strip().lower()
        if not needle:
            return False
        haystack = [self.title.lower(), self.isbn.lower()]
        haystack += [a.name.lower() for a in self.authors]
        haystack += [s.lower() for s in self.subjects]
        return any(needle in field_value for field_value in haystack)


@dataclass(slots=True)
class BookItem:
    """One barcoded copy. This is the contended row when a title has one copy left."""

    barcode: str
    book_id: str
    status: ItemStatus = ItemStatus.AVAILABLE
    borrower_id: str | None = None
    reserved_for: str | None = None
    due_on: date | None = None

    def is_borrowable_by(self, account_id: str) -> bool:
        if self.status is ItemStatus.AVAILABLE:
            return True
        return self.status is ItemStatus.RESERVED and self.reserved_for == account_id

    def lend_to(self, account_id: str, due_on: date) -> None:
        if not self.is_borrowable_by(account_id):
            raise ItemUnavailableError(f"copy {self.barcode} is {self.status}")
        self.status = ItemStatus.LOANED
        self.borrower_id = account_id
        self.reserved_for = None
        self.due_on = due_on

    def shelve(self) -> None:
        """Back on the open shelf: after a return with nobody waiting, or a repair."""
        self.status = ItemStatus.AVAILABLE
        self.borrower_id = None
        self.reserved_for = None
        self.due_on = None

    def put_on_hold_shelf(self, account_id: str) -> None:
        self.status = ItemStatus.RESERVED
        self.borrower_id = None
        self.reserved_for = account_id
        self.due_on = None

    def mark(self, status: ItemStatus) -> None:
        """LOST or DAMAGED, from any state a librarian can reach."""
        if status not in (ItemStatus.LOST, ItemStatus.DAMAGED):
            raise ItemStateError(f"{status} is not a librarian mark")
        self.status = status
        self.borrower_id = None
        self.reserved_for = None
        self.due_on = None


# --8<-- [end:catalog_models]


# --8<-- [start:people]
class Person(ABC):
    """A human the library knows about. Subclasses declare their own borrowing limit."""

    def __init__(self, person_id: str, name: str, email: str) -> None:
        if not name.strip():
            raise ValidationError("a person needs a name")
        self.id = person_id
        self.name = name.strip()
        self.email = email

    @property
    @abstractmethod
    def role(self) -> Role: ...

    @property
    @abstractmethod
    def max_loans(self) -> int: ...


class Member(Person):
    role = Role.MEMBER
    max_loans = 5


class Librarian(Person):
    role = Role.LIBRARIAN
    max_loans = 10


@dataclass(slots=True)
class Fine:
    id: str
    account_id: str
    barcode: str
    amount: Money
    reason: str
    assessed_on: date
    paid: bool = False


@dataclass(slots=True)
class Account:
    """The borrowing side of a person: what they hold, what they owe, whether they are blocked."""

    id: str
    holder_id: str
    role: Role
    max_loans: int
    status: AccountStatus = AccountStatus.ACTIVE
    borrowed: set[str] = field(default_factory=set)
    fines: list[Fine] = field(default_factory=list)

    def unpaid_total(self) -> Money:
        total = Money(0)
        for fine in self.fines:
            if not fine.paid:
                total = total + fine.amount
        return total


class AccountFactory:
    """Factory Method: the person's class decides the limit, so the desk never asks."""

    @staticmethod
    def open(account_id: str, person: Person) -> Account:
        return Account(
            id=account_id, holder_id=person.id, role=person.role, max_loans=person.max_loans
        )


# --8<-- [end:people]


# --8<-- [start:loans]
@dataclass(slots=True)
class Loan:
    id: str
    barcode: str
    book_id: str
    account_id: str
    borrowed_on: date
    due_on: date
    returned_on: date | None = None
    renewals: int = 0
    status: LoanStatus = LoanStatus.ACTIVE

    def days_overdue(self, today: date) -> int:
        """Derived, never stored: an overdue flag in a column goes stale at midnight."""
        reference = self.returned_on or today
        return max(0, (reference - self.due_on).days)

    def renew(self, today: date, days: int = LOAN_PERIOD_DAYS) -> date:
        if self.renewals >= MAX_RENEWALS:
            raise RenewalBlockedError(f"loan {self.id} has used its {MAX_RENEWALS} renewals")
        self.renewals += 1
        self.due_on = max(self.due_on, today) + timedelta(days=days)
        return self.due_on


@dataclass(slots=True)
class Reservation:
    """A hold on a *book*; whichever copy comes back first satisfies it."""

    id: str
    book_id: str
    account_id: str
    placed_at: float
    status: ReservationStatus = ReservationStatus.WAITING
    barcode: str | None = None
    pickup_by: date | None = None


# --8<-- [end:loans]
