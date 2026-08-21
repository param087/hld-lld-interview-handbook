"""The catalog behind a Repository, and a caching Proxy in front of it.

``Catalog`` and ``CachingCatalog`` implement the same ``SearchableCatalog`` protocol, so
the service layer cannot tell them apart - which is the whole point of a Proxy.
"""

from __future__ import annotations

import threading
from typing import Protocol

from lld.library_management.models import Book, BookItem, NotInCatalogError


# --8<-- [start:repository]
class BookRepository(Protocol):
    """Collection-like persistence boundary for catalog records and copies."""

    def add_book(self, book: Book) -> None: ...

    def add_item(self, item: BookItem) -> None: ...

    def book(self, book_id: str) -> Book: ...

    def item(self, barcode: str) -> BookItem: ...

    def books(self) -> list[Book]: ...

    def items_of(self, book_id: str) -> list[BookItem]: ...


class InMemoryBookRepository:
    """Dict-backed store. ``_lock`` guards the two dicts, not the item *status*."""

    def __init__(self) -> None:
        self._books: dict[str, Book] = {}
        self._items: dict[str, BookItem] = {}
        self._lock = threading.Lock()

    def add_book(self, book: Book) -> None:
        with self._lock:
            self._books[book.id] = book

    def add_item(self, item: BookItem) -> None:
        with self._lock:
            if item.book_id not in self._books:
                raise NotInCatalogError(f"no catalog record {item.book_id!r}")
            self._items[item.barcode] = item

    def book(self, book_id: str) -> Book:
        with self._lock:
            try:
                return self._books[book_id]
            except KeyError:
                raise NotInCatalogError(f"no book {book_id!r}") from None

    def item(self, barcode: str) -> BookItem:
        with self._lock:
            try:
                return self._items[barcode]
            except KeyError:
                raise NotInCatalogError(f"no copy with barcode {barcode!r}") from None

    def books(self) -> list[Book]:
        with self._lock:
            return list(self._books.values())

    def items_of(self, book_id: str) -> list[BookItem]:
        with self._lock:
            return sorted(
                (i for i in self._items.values() if i.book_id == book_id), key=lambda i: i.barcode
            )


# --8<-- [end:repository]


# --8<-- [start:catalog]
class SearchableCatalog(Protocol):
    """What the service layer needs. Both the real catalog and the proxy satisfy it."""

    def search(self, query: str) -> list[Book]: ...

    def book(self, book_id: str) -> Book: ...

    def item(self, barcode: str) -> BookItem: ...

    def copies(self, book_id: str) -> list[BookItem]: ...


class Catalog:
    """Search by title, author, ISBN or subject - one predicate on ``Book.matches``."""

    def __init__(self, repository: BookRepository | None = None) -> None:
        self._repository = repository or InMemoryBookRepository()

    def add_book(self, book: Book) -> None:
        self._repository.add_book(book)

    def add_copies(self, book_id: str, barcodes: list[str]) -> list[BookItem]:
        items = [BookItem(barcode=b, book_id=book_id) for b in barcodes]
        for item in items:
            self._repository.add_item(item)
        return items

    def search(self, query: str) -> list[Book]:
        return sorted(
            (b for b in self._repository.books() if b.matches(query)), key=lambda b: b.title
        )

    def book(self, book_id: str) -> Book:
        return self._repository.book(book_id)

    def item(self, barcode: str) -> BookItem:
        return self._repository.item(barcode)

    def copies(self, book_id: str) -> list[BookItem]:
        return self._repository.items_of(book_id)


class CachingCatalog:
    """Proxy: identical interface, memoised searches, invalidated when stock changes.

    Search is the hottest read path in an OPAC and the only one whose answer changes
    rarely. Availability deliberately is *not* cached - it changes every checkout.
    """

    def __init__(self, inner: Catalog) -> None:
        self._inner = inner
        self._cache: dict[str, list[Book]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def add_book(self, book: Book) -> None:
        self._inner.add_book(book)
        self.invalidate()

    def add_copies(self, book_id: str, barcodes: list[str]) -> list[BookItem]:
        items = self._inner.add_copies(book_id, barcodes)
        self.invalidate()
        return items

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def search(self, query: str) -> list[Book]:
        key = query.strip().lower()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self.hits += 1
                return list(cached)
        result = self._inner.search(query)
        with self._lock:
            self._cache[key] = list(result)
            self.misses += 1
        return result

    def book(self, book_id: str) -> Book:
        return self._inner.book(book_id)

    def item(self, barcode: str) -> BookItem:
        return self._inner.item(barcode)

    def copies(self, book_id: str) -> list[BookItem]:
        return self._inner.copies(book_id)


# --8<-- [end:catalog]
