"""Object-oriented Python for interviews: the constructs an LLD round asks for.

One domain runs through the module - the seat map of a show and the bookings made
against it - so every construct lands on the same objects: a value object (``Seat``),
an entity (``Booking``), a container (``SeatMap``), a structural interface
(``PricingRule``), a nominal base class (``Notifier``) and a generic ``Registry``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from enum import Flag, StrEnum, auto
from functools import partial, total_ordering
from types import TracebackType
from typing import Literal, Protocol, Self, override, runtime_checkable

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError

BASE_FARE = Money.of("8.00")
PREMIUM_EXTRA = Money.of("4.00")
BUSINESS_EXTRA = Money.of("12.00")


# --8<-- [start:enums]
class SeatClass(StrEnum):
    """``StrEnum`` members *are* strings: they serialise straight to JSON and compare
    equal to the raw value. ``auto()`` yields the lower-cased member name.
    """

    ECONOMY = auto()
    PREMIUM = auto()
    BUSINESS = auto()


class BookingStatus(StrEnum):
    HELD = auto()
    CONFIRMED = auto()
    CANCELLED = auto()


class Amenity(Flag):
    """``Flag`` is the enum for a *set* of options: members combine with ``|``, are
    tested with ``in``, and the whole set travels as one field.
    """

    NONE = 0
    WINDOW = auto()
    AISLE = auto()
    POWER = auto()
    EXTRA_LEGROOM = auto()
# --8<-- [end:enums]


# --8<-- [start:value]
@dataclass(frozen=True, slots=True, order=True)
class Seat:
    """A value object: identified by *what it is*, never by an id. ``frozen=True``
    gives a ``__hash__`` and makes it safe to share between threads, ``slots=True``
    drops ``__dict__``, ``order=True`` derives all six comparisons from field order.
    """

    row: int
    column: str
    seat_class: SeatClass = SeatClass.ECONOMY
    amenities: Amenity = field(default=Amenity.NONE, compare=False)

    def __post_init__(self) -> None:
        """Validate and normalise once, so nobody ever holds a broken ``Seat``."""
        if self.row < 1:
            raise ValidationError(f"row must be positive, got {self.row}")
        if len(self.column) != 1 or not self.column.isalpha():
            raise ValidationError(f"column must be a single letter, got {self.column!r}")
        object.__setattr__(self, "column", self.column.upper())

    @classmethod
    def parse(cls, label: str, seat_class: SeatClass = SeatClass.ECONOMY) -> Self:
        """A named constructor. ``Self`` keeps the annotation correct in subclasses."""
        row, column = label[:-1], label[-1:]
        if not row.isdigit():
            raise ValidationError(f"{label!r} is not a seat label such as '12A'")
        return cls(int(row), column, seat_class)

    @staticmethod
    def aisle_columns() -> frozenset[str]:
        """No ``self``, no ``cls``: a helper that belongs to the class by topic only."""
        return frozenset({"C", "D"})

    @property
    def label(self) -> str:
        """Computed, read-only, and no parentheses at the call site."""
        return f"{self.row}{self.column}"

    def upgraded(self, seat_class: SeatClass) -> Seat:
        """Immutable update: ``replace`` copies the object with one field changed."""
        return replace(self, seat_class=seat_class)

    def __str__(self) -> str:
        return f"{self.label} ({self.seat_class})"
# --8<-- [end:value]


# --8<-- [start:entity]
@dataclass(slots=True, eq=False)
class Booking:
    """An entity: identified by its id, mutable, with guarded transitions. ``eq=False``
    stops the dataclass generating a field-by-field ``__eq__``, which is wrong here -
    a booking that gains a seat is still the same booking.
    """

    booking_id: str
    show_id: str
    seats: list[Seat] = field(default_factory=list)
    status: BookingStatus = BookingStatus.HELD

    @property
    def seat_labels(self) -> list[str]:
        return [seat.label for seat in self.seats]

    def confirm(self) -> None:
        if self.status is not BookingStatus.HELD:
            raise InvalidStateError(f"booking {self.booking_id} is {self.status}, not held")
        if not self.seats:
            raise ValidationError("a booking needs at least one seat")
        self.status = BookingStatus.CONFIRMED

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Booking):
            return NotImplemented  # let the other operand try; never return False here
        return self.booking_id == other.booking_id

    def __hash__(self) -> int:
        return hash(self.booking_id)
# --8<-- [end:entity]


# --8<-- [start:ordering]
@total_ordering
@dataclass(frozen=True, slots=True, eq=False)
class WaitlistEntry:
    """When the natural order is *not* the field order, define one key, write ``__eq__``
    and ``__lt__`` over it, and let ``total_ordering`` derive the other four.
    """

    booking_id: str
    priority: int
    sequence: int

    def _key(self) -> tuple[int, int]:
        return (-self.priority, self.sequence)  # highest priority first, then arrival

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WaitlistEntry):
            return NotImplemented
        return self._key() == other._key()

    def __lt__(self, other: WaitlistEntry) -> bool:
        return self._key() < other._key()

    def __hash__(self) -> int:
        return hash(self._key())
# --8<-- [end:ordering]


# --8<-- [start:container]
class LabelMixin:
    """Behaviour and no state: a mixin adds methods, never an ``__init__``. Annotating
    ``self`` documents the only thing the host class must provide.
    """

    def labels(self: Iterable[Seat]) -> list[str]:
        return [seat.label for seat in self]


class SeatMap(LabelMixin):
    """A container: ``len()``, ``for``, ``in``, ``[]`` and ``repr()`` all route through
    the dunders below, so ``SeatMap`` never needs a ``get_all_seats()`` accessor.
    """

    def __init__(self, seats: Iterable[Seat]) -> None:
        self._seats: dict[str, Seat] = {seat.label: seat for seat in sorted(seats)}

    def __len__(self) -> int:
        return len(self._seats)

    def __iter__(self) -> Iterator[Seat]:
        return iter(self._seats.values())

    def __contains__(self, item: object) -> bool:
        """Accept a ``Seat`` or a label, so ``"2C" in seat_map`` reads naturally."""
        if isinstance(item, Seat):
            return item.label in self._seats
        return isinstance(item, str) and item.upper() in self._seats

    def __getitem__(self, label: str) -> Seat:
        try:
            return self._seats[label.upper()]
        except KeyError:
            raise NotFoundError(f"no seat {label!r} on this map") from None

    @property
    def rows(self) -> int:
        return len({seat.row for seat in self._seats.values()})

    def __repr__(self) -> str:
        """Unambiguous, for logs and pdb - not a user-facing string."""
        return f"SeatMap(rows={self.rows}, seats={len(self)})"
# --8<-- [end:container]


# --8<-- [start:context]
class SeatHold:
    """A context manager: whatever happens inside the block, the hold is released.
    ``__exit__`` returns ``None`` (falsy), so an exception inside still propagates.
    """

    def __init__(self, seat_map: SeatMap, labels: Sequence[str], held: set[str]) -> None:
        self._seat_map = seat_map
        self._labels = [label.upper() for label in labels]
        self._held = held
        self.seats: list[Seat] = []

    def __enter__(self) -> Self:
        clash = sorted(set(self._labels) & self._held)
        if clash:
            raise ConflictError(f"already held: {', '.join(clash)}")
        self.seats = [self._seat_map[label] for label in self._labels]
        self._held.update(self._labels)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._held.difference_update(self._labels)
# --8<-- [end:context]


# --8<-- [start:interfaces]
@runtime_checkable
class PricingRule(Protocol):
    """Structural typing: anything with this method qualifies, including a test double
    defined three lines into a test. Nobody inherits from it.
    """

    def price(self, seat: Seat) -> Money: ...


@dataclass(frozen=True, slots=True)
class ClassPricing:
    """Satisfies ``PricingRule`` without importing it: no base class, no registry."""

    base: Money = BASE_FARE

    def price(self, seat: Seat) -> Money:
        extra = {
            SeatClass.ECONOMY: Money(0),
            SeatClass.PREMIUM: PREMIUM_EXTRA,
            SeatClass.BUSINESS: BUSINESS_EXTRA,
        }[seat.seat_class]
        return self.base + extra


@dataclass(frozen=True, slots=True)
class FlatPricing:
    fare: Money = BASE_FARE

    def price(self, seat: Seat) -> Money:
        return self.fare


class Notifier(ABC):
    """Nominal typing: subclasses declare the relationship and inherit ``notify``,
    the shared algorithm. ``render`` is the one hook a subclass must supply;
    forgetting it makes the subclass uninstantiable.
    """

    def notify(self, booking: Booking) -> str:
        return f"[{self.channel()}] {self.render(booking)}"

    @abstractmethod
    def render(self, booking: Booking) -> str: ...

    def channel(self) -> str:
        return type(self).__name__.removesuffix("Notifier").lower()


class EmailNotifier(Notifier):
    @override  # a typo in the name is now an error, not a silently dead method
    def render(self, booking: Booking) -> str:
        return f"Booking {booking.booking_id}: {', '.join(booking.seat_labels)}"


class SmsNotifier(Notifier):
    @override
    def render(self, booking: Booking) -> str:
        return f"{booking.booking_id} confirmed for {len(booking.seats)} seat(s)"
# --8<-- [end:interfaces]


# --8<-- [start:generics]
type SeatPredicate = Callable[[Seat], bool]


class Registry[T]:
    """PEP 695 generics: ``class Registry[T]`` needs no ``TypeVar`` line. The key
    function is injected, so the registry never assumes an ``id`` attribute."""

    def __init__(self, key: Callable[[T], str]) -> None:
        self._key = key
        self._items: dict[str, T] = {}

    def add(self, item: T) -> Self:
        """Returning ``Self`` (not ``Registry``) keeps chaining typed in subclasses."""
        self._items[self._key(item)] = item
        return self

    def get(self, key: str) -> T:
        try:
            return self._items[key]
        except KeyError:
            raise NotFoundError(f"no item {key!r} in the registry") from None

    def list(self, order: Literal["insertion", "key"] = "insertion") -> list[T]:
        """``Literal`` pins the accepted strings without inventing an enum for two."""
        keys = list(self._items) if order == "insertion" else sorted(self._items)
        return [self._items[key] for key in keys]


def seats_matching(seat_map: SeatMap, predicate: SeatPredicate) -> list[Seat]:
    return [seat for seat in seat_map if predicate(seat)]
# --8<-- [end:generics]


# --8<-- [start:composition]
class AuditedSeatMap:
    """Composition over inheritance: hold a ``SeatMap``, expose the same call surface,
    add the audit. Subclassing would inherit every method the base grows later.
    """

    def __init__(self, inner: SeatMap) -> None:
        self._inner = inner
        self._reads: list[str] = []

    def __len__(self) -> int:
        return len(self._inner)

    def __iter__(self) -> Iterator[Seat]:
        return iter(self._inner)

    def __contains__(self, item: object) -> bool:
        return item in self._inner

    def __getitem__(self, label: str) -> Seat:
        self._reads.append(label.upper())
        return self._inner[label]

    @property
    def reads(self) -> tuple[str, ...]:
        return tuple(self._reads)
# --8<-- [end:composition]


# --8<-- [start:gotchas]
def hold_labels(labels: Sequence[str], into: list[str] | None = None) -> list[str]:
    """Defaults are evaluated once, at ``def`` time, so a ``[]`` default is shared by
    every call that omits it. Take ``None`` and allocate inside."""
    result = [] if into is None else into
    result.extend(label.upper() for label in labels)
    return result


def _is_in_row(row: int, seat: Seat) -> bool:
    return seat.row == row


def row_pickers(rows: Sequence[int]) -> list[SeatPredicate]:
    """Closures capture the *variable*, not its value, so lambdas built in a loop all
    see the final row. ``partial`` binds the value now."""
    return [partial(_is_in_row, row) for row in rows]


def identity_vs_equality(label: str) -> tuple[bool, bool, bool]:
    """``is`` asks "the same object?", ``==`` asks "the same value?". Enum members are
    singletons, so ``is`` is the right test for them and only for them."""
    parsed, twin = Seat.parse(label), Seat.parse(label)
    return parsed is twin, parsed == twin, parsed.seat_class is SeatClass.ECONOMY
# --8<-- [end:gotchas]


def main() -> None:
    layout = ((1, SeatClass.BUSINESS), (2, SeatClass.PREMIUM), (3, SeatClass.ECONOMY))
    seats = [
        Seat(row, column, seat_class, Amenity.WINDOW if column in "AD" else Amenity.AISLE)
        for row, seat_class in layout
        for column in "ABCD"
    ]
    seat_map = SeatMap(seats)
    print(f"--- {seat_map!r} ---")
    print(f"mixin labels: {', '.join(seat_map.labels()[:4])} ...; '2c' in map -> {'2c' in seat_map}")

    pricing: PricingRule = ClassPricing()
    quotes = ", ".join(f"{seat_map[x]} = {pricing.price(seat_map[x])}" for x in ("1A", "2B", "3C"))
    print(f"prices: {quotes}")

    aisle = seat_map["2C"]
    print(f"{aisle.label}: amenities {aisle.amenities.name}, AISLE in them -> {Amenity.AISLE in aisle.amenities}")
    print(f"replace: {aisle} -> {aisle.upgraded(SeatClass.BUSINESS)}; original still {aisle}")

    held: set[str] = set()
    booking = Booking("BK-1", "SHOW-7")
    with SeatHold(seat_map, ["3C", "3D"], held) as hold:
        booking.seats.extend(hold.seats)
        print(f"inside the with block, held = {sorted(held)}")
    print(f"after the with block, held = {sorted(held)}")

    booking.confirm()
    print(f"{booking.booking_id} is {booking.status} for {', '.join(booking.seat_labels)}")
    print(EmailNotifier().notify(booking))
    print(SmsNotifier().notify(booking))

    registry: Registry[Booking] = Registry(key=lambda item: item.booking_id)
    registry.add(booking).add(Booking("BK-2", "SHOW-7", [seat_map["1A"]]))
    print(f"registry by key: {[item.booking_id for item in registry.list(order='key')]}")

    waitlist = [WaitlistEntry("BK-3", 1, 2), WaitlistEntry("BK-4", 5, 3), WaitlistEntry("BK-5", 5, 1)]
    print(f"waitlist order: {[entry.booking_id for entry in sorted(waitlist)]}")

    audited = AuditedSeatMap(seat_map)
    _ = audited["1A"], audited["2b"]
    print(f"composition: {len(audited)} seats wrapped, reads audited {list(audited.reads)}")

    same, equal, singleton = identity_vs_equality("2B")
    print(f"is -> {same}; == -> {equal}; enum member is a singleton -> {singleton}")
    picked = [sorted({seat.row for seat in seats_matching(seat_map, p)}) for p in row_pickers([1, 3])]
    print(f"each picker kept its own row: {picked}")
    print(f"no shared default: {hold_labels(['4a'])} then {hold_labels(['4b'])}")

    try:
        Seat.parse("row twelve")
    except ValidationError as exc:
        print(f"rejected: {exc}")


if __name__ == "__main__":
    main()
