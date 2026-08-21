"""The physical room roster and the Builder that assembles it.

``Hotel`` has no lock of its own on purpose: every mutation of a ``Room`` happens
inside ``AvailabilityService.types_locked``, so the room-type lock protects the roster
and the night counters together.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from common import ValidationError
from lld.hotel_management.models import Room, RoomStatus, RoomType, UnknownReservationError


# --8<-- [start:hotel]
class Hotel:
    """A named property with a fixed set of rooms and a tax rate."""

    def __init__(self, name: str, rooms: list[Room], tax_rate: Decimal = Decimal("0.12")) -> None:
        self.name = name
        self.tax_rate = tax_rate
        self._rooms: dict[str, Room] = {r.number: r for r in rooms}

    def room(self, number: str) -> Room:
        try:
            return self._rooms[number]
        except KeyError:
            raise UnknownReservationError(f"{self.name} has no room {number!r}") from None

    def all_rooms(self) -> list[Room]:
        return list(self._rooms.values())

    def rooms_of(self, room_type: RoomType) -> list[Room]:
        return [r for r in self._rooms.values() if r.type is room_type]

    def inventory(self) -> dict[RoomType, int]:
        """Sellable capacity per type: rooms out of service are not inventory."""
        counts: dict[RoomType, int] = {}
        for room in self._rooms.values():
            if room.status is not RoomStatus.OUT_OF_SERVICE:
                counts[room.type] = counts.get(room.type, 0) + 1
        return counts

    def first_ready(self, room_type: RoomType) -> Room | None:
        """Lowest-numbered clean room of the type, or None. Callers hold the type lock."""
        ready = [r for r in self.rooms_of(room_type) if r.status is RoomStatus.AVAILABLE]
        return min(ready, key=lambda r: r.number) if ready else None


class HotelBuilder:
    """Stepwise construction with validation - the one place Builder earns its keep.

    >>> hotel = HotelBuilder().named("Seaside").with_rooms(RoomType.DOUBLE, 2, floor=1).build()
    >>> len(hotel.all_rooms())
    2
    """

    def __init__(self) -> None:
        self._name: str | None = None
        self._tax_rate = Decimal("0.12")
        self._rooms: list[Room] = []

    def named(self, name: str) -> Self:
        self._name = name.strip()
        return self

    def with_tax_rate(self, rate: Decimal) -> Self:
        if rate < 0:
            raise ValidationError("tax rate cannot be negative")
        self._tax_rate = rate
        return self

    def with_rooms(self, room_type: RoomType, count: int, floor: int) -> Self:
        """Add ``count`` rooms of one type on one floor, numbered ``<floor><nn>``."""
        if count < 1:
            raise ValidationError("count must be at least 1")
        existing = sum(1 for r in self._rooms if r.floor == floor)
        for i in range(count):
            self._rooms.append(Room(number=f"{floor}{existing + i + 1:02d}", floor=floor, type=room_type))
        return self

    def build(self) -> Hotel:
        if not self._name:
            raise ValidationError("a hotel needs a name")
        if not self._rooms:
            raise ValidationError("a hotel needs at least one room")
        numbers = [r.number for r in self._rooms]
        if len(set(numbers)) != len(numbers):
            raise ValidationError(f"duplicate room numbers: {sorted(numbers)}")
        return Hotel(self._name, self._rooms, self._tax_rate)


# --8<-- [end:hotel]
