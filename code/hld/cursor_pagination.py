"""Opaque cursor pagination: keyset queries over a sorted in-memory table.

What the module demonstrates, in the order an interviewer asks about it:

* ``CursorCodec`` turns the sort key of the last row on a page, ``(created_at, id)``, plus a
  fingerprint of the query it belongs to into an opaque, URL-safe, HMAC-signed token. Clients
  cannot forge a position, replay a cursor against a different filter, or depend on its layout.
* ``OrderTable`` stands in for an indexed table: rows sorted by ``(created_at, id)``, one list
  per filter value as a composite index. ``page_by_keyset`` is one ``bisect`` and a slice, the
  in-memory twin of ``WHERE (created_at, id) < (?, ?) ORDER BY created_at DESC, id DESC
  LIMIT n + 1``; ``page_by_offset`` is the ``OFFSET`` scan kept for contrast.
* ``walk_keyset`` and ``walk_offset`` page through the table while rows arrive, which is how
  the demo and the tests show that keyset pages never repeat or skip a row and offset pages do.
"""

from __future__ import annotations

import base64
import binascii
import bisect
import hashlib
import hmac
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass

from common import ConflictError, FakeClock, ValidationError

# --8<-- [start:cursor]
SIGNATURE_BYTES = 12  # a 96-bit tag: short enough for a URL, far too long to forge


def query_fingerprint(customer_id: str | None) -> str:
    """Short hash of the filter and sort a page belongs to; it travels inside the cursor."""
    canonical = json.dumps({"customer_id": customer_id, "sort": "-created_at"}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class Cursor:
    """The boundary of a page: the sort key of its last row and the query it was computed for."""

    created_at: int  # epoch milliseconds
    id: str
    query: str


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True, slots=True)
class CursorCodec:
    """``base64url(payload).base64url(hmac)``: opaque to clients, tamper-evident to the server.

    The secret never leaves the server (load it from a secret store; rotate it by accepting
    two keys for a while). Opacity is what lets you change the payload later without breaking
    a single client, and the signature is what stops a client from editing the boundary or
    the query fingerprint.
    """

    secret: bytes

    def encode(self, cursor: Cursor) -> str:
        payload = json.dumps([cursor.created_at, cursor.id, cursor.query], separators=(",", ":"))
        raw = payload.encode()
        return f"{_b64encode(raw)}.{_b64encode(self._sign(raw))}"

    def decode(self, token: str) -> Cursor:
        try:
            body, tag = token.split(".", 1)
            raw, given = _b64decode(body), _b64decode(tag)
        except (ValueError, binascii.Error) as exc:
            raise ValidationError("malformed cursor") from exc
        if not hmac.compare_digest(given, self._sign(raw)):
            raise ValidationError("cursor signature mismatch")
        try:
            created_at, order_id, query = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValidationError("malformed cursor") from exc
        if not (
            isinstance(created_at, int) and isinstance(order_id, str) and isinstance(query, str)
        ):
            raise ValidationError("malformed cursor")
        return Cursor(created_at, order_id, query)

    def _sign(self, raw: bytes) -> bytes:
        return hmac.new(self.secret, raw, hashlib.sha256).digest()[:SIGNATURE_BYTES]


# --8<-- [end:cursor]


# --8<-- [start:table]
@dataclass(frozen=True, slots=True)
class Order:
    id: str
    customer_id: str
    created_at: int  # epoch milliseconds

    @property
    def sort_key(self) -> tuple[int, str]:
        """Newest first, ties broken by id, so the order is total and every boundary unambiguous."""
        return (self.created_at, self.id)


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[Order, ...]
    next_cursor: str | None  # None on the last page; offset pages never carry one
    rows_examined: int  # what the storage engine had to read to build this page


def _sort_key(order: Order) -> tuple[int, str]:
    return order.sort_key


class OrderTable:
    """In-memory stand-in for an orders table with an index on ``(created_at, id)``.

    ``_all`` holds every order in ascending key order; ``_by_customer`` keeps one such list per
    customer, the equivalent of a composite index ``(customer_id, created_at, id)``, so a
    filtered page is still a seek and not a scan. ``_lock`` guards ``_all``, ``_by_customer``
    and ``_ids``: readers hold it only while copying a slice, writers while inserting.
    """

    def __init__(self, codec: CursorCodec, max_limit: int = 100) -> None:
        if max_limit <= 0:
            raise ValidationError("max_limit must be positive")
        self._codec = codec
        self._max_limit = max_limit
        self._all: list[Order] = []
        self._by_customer: dict[str, list[Order]] = {}
        self._ids: set[str] = set()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._all)

    def insert(self, order: Order) -> None:
        with self._lock:
            if order.id in self._ids:
                raise ConflictError(f"order {order.id!r} already exists")
            self._ids.add(order.id)
            bisect.insort(self._all, order, key=_sort_key)
            bisect.insort(self._by_customer.setdefault(order.customer_id, []), order, key=_sort_key)

    def page_by_keyset(
        self, limit: int, cursor: str | None = None, customer_id: str | None = None
    ) -> Page:
        """Newest first, continuing strictly after the cursor's row: one seek and ``limit + 1`` rows."""
        limit = self._clamp(limit)
        query = query_fingerprint(customer_id)
        boundary: tuple[int, str] | None = None
        if cursor is not None:
            decoded = self._codec.decode(cursor)
            if decoded.query != query:
                raise ValidationError("cursor belongs to a different query")
            boundary = (decoded.created_at, decoded.id)
        with self._lock:
            rows = self._index(customer_id)
            end = (
                len(rows) if boundary is None else bisect.bisect_left(rows, boundary, key=_sort_key)
            )
            window = rows[max(0, end - limit - 1) : end]
        window.reverse()
        items = tuple(window[:limit])
        next_cursor = None
        if len(window) > limit:  # the extra row proves a next page exists, no COUNT(*) needed
            last = items[-1]
            next_cursor = self._codec.encode(Cursor(last.created_at, last.id, query))
        return Page(items, next_cursor, rows_examined=len(window))

    def page_by_offset(self, limit: int, offset: int = 0, customer_id: str | None = None) -> Page:
        """The naive scheme: the engine reads ``offset + limit`` rows and throws ``offset`` away."""
        limit = self._clamp(limit)
        if offset < 0:
            raise ValidationError("offset must be non-negative")
        with self._lock:
            rows = self._index(customer_id)
            total = len(rows)
            end = max(0, total - offset)
            window = rows[max(0, end - limit) : end]
        window.reverse()
        return Page(tuple(window), None, rows_examined=min(offset, total) + len(window))

    def _index(self, customer_id: str | None) -> list[Order]:
        return self._all if customer_id is None else self._by_customer.get(customer_id, [])

    def _clamp(self, limit: int) -> int:
        if limit <= 0:
            raise ValidationError("limit must be positive")
        return min(limit, self._max_limit)  # clamp silently, the way most public APIs do


# --8<-- [end:table]


# --8<-- [start:walk]
@dataclass(frozen=True, slots=True)
class WalkStats:
    """What a client saw while paging through the whole table."""

    ids: tuple[str, ...]
    pages: int
    rows_examined: int

    @property
    def duplicates(self) -> int:
        return len(self.ids) - len(set(self.ids))


def walk_keyset(
    table: OrderTable,
    limit: int,
    customer_id: str | None = None,
    between_pages: Callable[[int], None] | None = None,
) -> WalkStats:
    """Follow ``next_cursor`` to the end; ``between_pages(n)`` runs after page ``n`` has been read."""
    ids: list[str] = []
    pages = examined = 0
    cursor: str | None = None
    while True:
        page = table.page_by_keyset(limit, cursor=cursor, customer_id=customer_id)
        pages += 1
        examined += page.rows_examined
        ids.extend(order.id for order in page.items)
        if between_pages is not None:
            between_pages(pages)
        cursor = page.next_cursor
        if cursor is None:
            return WalkStats(tuple(ids), pages, examined)


def walk_offset(
    table: OrderTable,
    limit: int,
    customer_id: str | None = None,
    between_pages: Callable[[int], None] | None = None,
) -> WalkStats:
    """Advance ``offset`` by ``limit`` until a short page arrives, the way offset clients do."""
    ids: list[str] = []
    pages = examined = offset = 0
    while True:
        page = table.page_by_offset(limit, offset=offset, customer_id=customer_id)
        pages += 1
        examined += page.rows_examined
        ids.extend(order.id for order in page.items)
        if between_pages is not None:
            between_pages(pages)
        if len(page.items) < limit:
            return WalkStats(tuple(ids), pages, examined)
        offset += limit


# --8<-- [end:walk]


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    table = OrderTable(CursorCodec(secret=b"load-me-from-a-secret-store"))
    customers = ("ann", "bob", "cat")
    for i in range(1, 10_001):
        if i % 3 == 1:
            clock.advance(1)  # three orders share every timestamp: ties are the normal case
        table.insert(Order(f"ord-{i:05d}", customers[i % 3], int(clock.now() * 1000)))
    print(f"table: {len(table):,} orders, 3 per second, newest first by (created_at, id)")

    first = table.page_by_keyset(limit=20)
    cursor = first.next_cursor or ""
    print(
        f"keyset page 1: {first.items[0].id} .. {first.items[-1].id}, {first.rows_examined} rows examined"
    )
    print(f"  next_cursor = {cursor[:28]}... ({len(cursor)} chars, base64url payload + HMAC tag)")
    offset_first = table.page_by_offset(limit=20, offset=0)

    clock.advance(1)
    table.insert(Order("ord-10001", "ann", int(clock.now() * 1000)))
    print("a new order ord-10001 arrives before page 2 is requested")
    second = table.page_by_keyset(limit=20, cursor=cursor)
    print(
        f"keyset page 2: {second.items[0].id} .. {second.items[-1].id}  "
        f"(continues after {first.items[-1].id}: no repeat, no skip)"
    )
    offset_second = table.page_by_offset(limit=20, offset=20)
    repeated = sorted({o.id for o in offset_first.items} & {o.id for o in offset_second.items})
    print(
        f"offset page 2: {offset_second.items[0].id} .. {offset_second.items[-1].id}  "
        f"(repeats {repeated[0]}: every row shifted by one)"
    )

    deep = table.page_by_offset(limit=20, offset=9_000)
    print(f"offset 9,000: {deep.rows_examined:,} rows examined to return {len(deep.items)}")
    by_keyset = walk_keyset(table, limit=20)
    by_offset = walk_offset(table, limit=20)
    print(
        f"full walk, keyset: {by_keyset.pages} pages, {by_keyset.rows_examined:,} rows examined, "
        f"{by_keyset.duplicates} duplicates"
    )
    print(
        f"full walk, offset: {by_offset.pages} pages, {by_offset.rows_examined:,} rows examined, "
        f"{by_offset.duplicates} duplicates"
    )

    ann = table.page_by_keyset(limit=20, customer_id="ann")
    print(
        f"filter customer_id=ann: {ann.items[0].id}, {ann.items[1].id}, ... "
        f"({ann.rows_examined} rows examined through the per-customer index)"
    )
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    for label, token, customer in (
        ("ann's cursor without the filter", ann.next_cursor or "", None),
        ("cursor with one edited character", tampered, None),
        ("page number sent as a cursor", "2", None),
    ):
        try:
            table.page_by_keyset(limit=20, cursor=token, customer_id=customer)
        except ValidationError as exc:
            print(f"{label} -> 400 {exc}")


if __name__ == "__main__":
    main()
