"""Interfaces and contracts: what a service promises, and how the promise is written down.

The domain is stock reservation - the call an e-commerce checkout makes before it takes
money. It carries every contract decision worth arguing about in an LLD round: command and
view DTOs, expected outcomes as result objects, broken promises as exceptions, an invariant
that is re-checked, an idempotency key, keyset pagination and an additive response schema.
"""

from __future__ import annotations

import itertools
import threading
from bisect import bisect_right
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Protocol, Self, runtime_checkable

from common import Clock, FakeClock, IdGenerator, NotFoundError, ValidationError

HOLD_SECONDS = 15 * 60
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
CURRENT_SCHEMA_VERSION = 2


# --8<-- [start:dto]
class RejectionReason(StrEnum):
    OUT_OF_STOCK = auto()
    ITEM_WITHDRAWN = auto()


@dataclass(frozen=True, slots=True)
class ReserveStock:
    """The command DTO: everything the call needs, checked once at the boundary.

    A frozen dataclass beats four positional arguments - the signature stops growing,
    the caller cannot swap two strings, and ``replace`` builds a variant in one line.
    """

    sku: str
    quantity: int
    order_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValidationError(f"quantity must be positive, got {self.quantity}")
        if not (self.sku and self.order_id and self.idempotency_key):
            raise ValidationError("sku, order_id and idempotency_key are all required")


@dataclass(frozen=True, slots=True)
class Reserved:
    """The success outcome. Carries the id the client needs for the next call."""

    reservation_id: str
    sku: str
    quantity: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class Rejected:
    """The *expected* failure. A value, not an exception: the caller must handle it,
    and it carries the number that lets the caller decide what to do next.
    """

    sku: str
    reason: RejectionReason
    available: int


type ReservationResult = Reserved | Rejected
# --8<-- [end:dto]


# --8<-- [start:invariants]
@dataclass(slots=True)
class StockItem:
    """Invariant: ``0 <= reserved <= on_hand`` at every observable moment.

    Preconditions are guard clauses that raise a domain error - the caller broke the
    contract. The invariant is re-checked before returning; if it fails, *this* method
    is the bug, so it raises ``AssertionError`` rather than a domain error.
    """

    sku: str
    on_hand: int
    reserved: int = 0
    withdrawn: bool = False

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved

    def reserve(self, quantity: int) -> None:
        """Pre: ``0 < quantity <= available``. Post: ``available`` drops by exactly that."""
        if quantity <= 0:
            raise ValidationError(f"quantity must be positive, got {quantity}")
        if quantity > self.available:
            raise ValidationError(f"only {self.available} of {self.sku} available")
        self.reserved += quantity
        self._check_invariant()

    def release(self, quantity: int) -> None:
        """Pre: ``0 < quantity <= reserved``. Post: ``available`` rises by exactly that."""
        if not 0 < quantity <= self.reserved:
            raise ValidationError(f"cannot release {quantity} of {self.reserved} reserved")
        self.reserved -= quantity
        self._check_invariant()

    def _check_invariant(self) -> None:
        if not 0 <= self.reserved <= self.on_hand:
            raise AssertionError(f"{self.sku}: 0 <= {self.reserved} <= {self.on_hand} is false")
# --8<-- [end:invariants]


# --8<-- [start:pagination]
@dataclass(frozen=True, slots=True)
class Page[T]:
    """A page is a value: the items plus the cursor that resumes after the last one.

    ``next_cursor is None`` means "that was the end". An empty page with a cursor is
    legal, so the field is a cursor rather than a ``has_more`` boolean.
    """

    items: tuple[T, ...]
    next_cursor: str | None = None


class SortableIdGenerator:
    """Zero-padded ids, because a keyset cursor must be unique *and* totally ordered."""

    def __init__(self, prefix: str = "RES", width: int = 4) -> None:
        self._prefix = prefix
        self._width = width
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def next_id(self) -> str:
        with self._lock:
            return f"{self._prefix}-{next(self._counter):0{self._width}d}"


class InMemoryReservationLog:
    """Keyset pagination: order by a unique immutable key, resume strictly after it.

    An offset drifts when rows are inserted between two pages; a cursor over the id
    does not, which is why repositories in this handbook page by cursor.
    """

    def __init__(self) -> None:
        self._by_sku: dict[str, list[Reserved]] = {}

    def append(self, reserved: Reserved) -> None:
        self._by_sku.setdefault(reserved.sku, []).append(reserved)

    def page_for_sku(
        self, sku: str, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
    ) -> Page[Reserved]:
        if not 0 < limit <= MAX_PAGE_SIZE:
            raise ValidationError(f"limit must be in 1..{MAX_PAGE_SIZE}, got {limit}")
        rows = sorted(self._by_sku.get(sku, []), key=lambda row: row.reservation_id)
        start = 0 if cursor is None else bisect_right([row.reservation_id for row in rows], cursor)
        window = tuple(rows[start : start + limit])
        exhausted = start + limit >= len(rows)
        return Page(window, None if exhausted or not window else window[-1].reservation_id)
# --8<-- [end:pagination]


# --8<-- [start:protocols]
@runtime_checkable
class StockRepository(Protocol):
    """Two methods, because two is all the service calls."""

    def get(self, sku: str) -> StockItem: ...

    def save(self, item: StockItem) -> None: ...


class ReservationLog(Protocol):
    """Append and read: the service is given no way to delete history."""

    def append(self, reserved: Reserved) -> None: ...

    def page_for_sku(self, sku: str, limit: int, cursor: str | None) -> Page[Reserved]: ...


class InMemoryStockRepository:
    def __init__(self, items: dict[str, StockItem] | None = None) -> None:
        self._items: dict[str, StockItem] = dict(items or {})

    def get(self, sku: str) -> StockItem:
        try:
            return self._items[sku]
        except KeyError:
            raise NotFoundError(f"no stock record for {sku!r}") from None

    def save(self, item: StockItem) -> None:
        self._items[item.sku] = item
# --8<-- [end:protocols]


# --8<-- [start:service]
class ReservationService:
    """One intention-revealing method per use case, and a contract for each.

    ``reserve`` is idempotent: replaying a command with the same ``idempotency_key``
    returns the first result instead of taking stock twice, which is what makes a
    client retry after a timeout safe. It raises only when the *caller* is wrong
    (unknown sku, malformed command) and returns ``Rejected`` when the *domain* says
    no, so an out-of-stock answer never travels as a stack trace.
    """

    def __init__(
        self,
        stock: StockRepository,
        log: ReservationLog,
        ids: IdGenerator,
        clock: Clock,
    ) -> None:
        self._stock = stock
        self._log = log
        self._ids = ids
        self._clock = clock
        self._results: dict[str, ReservationResult] = {}
        self._lock = threading.Lock()  # guards _results and the read-modify-write on stock

    def reserve(self, command: ReserveStock) -> ReservationResult:
        with self._lock:
            replayed = self._results.get(command.idempotency_key)
            if replayed is not None:
                return replayed
            item = self._stock.get(command.sku)
            result = self._decide(item, command)
            self._results[command.idempotency_key] = result
            return result

    def _decide(self, item: StockItem, command: ReserveStock) -> ReservationResult:
        if item.withdrawn:
            return Rejected(command.sku, RejectionReason.ITEM_WITHDRAWN, item.available)
        if command.quantity > item.available:
            return Rejected(command.sku, RejectionReason.OUT_OF_STOCK, item.available)
        item.reserve(command.quantity)
        self._stock.save(item)
        reserved = Reserved(
            self._ids.next_id(), command.sku, command.quantity, self._clock.now() + HOLD_SECONDS
        )
        self._log.append(reserved)
        return reserved

    def reservations_for(
        self, sku: str, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
    ) -> Page[Reserved]:
        return self._log.page_for_sku(sku, limit, cursor)
# --8<-- [end:service]


# --8<-- [start:versioning]
@dataclass(frozen=True, slots=True)
class ReservationView:
    """The response DTO: additive, versioned, and never the domain object itself.

    A new field arrives with a default so an old client keeps working. A field is
    never removed or re-typed in place - that is a new version of the resource, not
    an edit to this one.
    """

    reservation_id: str
    sku: str
    quantity: int
    expires_at: float
    schema_version: int = CURRENT_SCHEMA_VERSION
    warehouse: str | None = None  # added in v2; absent on records written by v1

    @classmethod
    def of(cls, reserved: Reserved, warehouse: str | None = None) -> Self:
        return cls(
            reserved.reservation_id,
            reserved.sku,
            reserved.quantity,
            reserved.expires_at,
            warehouse=warehouse,
        )

    def to_payload(self) -> dict[str, object]:
        """Omit unset optional fields instead of sending nulls: a client that has not
        heard of ``warehouse`` sees the same document it saw before v2.
        """
        payload: dict[str, object] = {
            "reservation_id": self.reservation_id,
            "sku": self.sku,
            "quantity": self.quantity,
            "expires_at": self.expires_at,
            "schema_version": self.schema_version,
        }
        if self.warehouse is not None:
            payload["warehouse"] = self.warehouse
        return payload
# --8<-- [end:versioning]


def build_service(on_hand: dict[str, int], clock: Clock) -> ReservationService:
    items = {sku: StockItem(sku, quantity) for sku, quantity in on_hand.items()}
    return ReservationService(
        stock=InMemoryStockRepository(items),
        log=InMemoryReservationLog(),
        ids=SortableIdGenerator(),
        clock=clock,
    )


def main() -> None:
    clock = FakeClock(start=1_700_000_000.0)
    service = build_service({"SKU-A": 10, "SKU-B": 1}, clock)

    command = ReserveStock(sku="SKU-A", quantity=3, order_id="ORD-1", idempotency_key="key-1")
    first = service.reserve(command)
    assert isinstance(first, Reserved)
    print(f"--- reserve 3 of SKU-A -> {first.reservation_id}, holds until {first.expires_at} ---")
    print(f"same idempotency key replayed -> the same result object: {service.reserve(command) is first}")

    for index in range(2, 6):
        service.reserve(ReserveStock("SKU-A", 1, f"ORD-{index}", f"key-{index}"))

    match service.reserve(ReserveStock("SKU-B", 5, "ORD-9", "key-9")):
        case Reserved(reservation_id=reservation_id):
            print(f"reserved {reservation_id}")
        case Rejected(reason=reason, available=available):
            print(f"expected failure is a value: {reason}, {available} left - no exception raised")

    print("--- keyset pagination over the reservation log ---")
    cursor: str | None = None
    while True:
        page = service.reservations_for("SKU-A", limit=2, cursor=cursor)
        print(f"cursor={cursor!r} -> {[row.reservation_id for row in page.items]}")
        cursor = page.next_cursor
        if cursor is None:
            break

    view = ReservationView.of(first, warehouse="LON-1")
    print(f"v2 payload: {view.to_payload()}")
    print(f"v1-shaped payload omits the new field: {ReservationView.of(first).to_payload()}")

    try:
        service.reserve(ReserveStock("SKU-MISSING", 1, "ORD-10", "key-10"))
    except NotFoundError as exc:
        print(f"broken precondition raises: {exc}")
    try:
        ReserveStock("SKU-A", 0, "ORD-11", "key-11")
    except ValidationError as exc:
        print(f"the DTO refuses to exist: {exc}")


if __name__ == "__main__":
    main()
