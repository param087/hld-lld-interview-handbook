"""A striped uniform-grid index of driver positions: the read path of dispatch."""

from __future__ import annotations

import threading
import zlib
from math import ceil, cos, floor, radians

from lld.ride_sharing.models import KM_PER_DEGREE, Location

Cell = tuple[int, int]


# --8<-- [start:index]
class DriverLocationIndex:
    """Which drivers are near a point, kept correct under a storm of GPS pings.

    Two *striped* lock arrays, never a single global lock:

    * ``_cell_locks[i]`` guards ``_cell_members[i]``, the cell to driver-id sets
      whose cells hash to stripe ``i``. Two drivers moving in different parts of
      the city touch different stripes and never wait for each other.
    * ``_driver_locks[j]`` guards ``_driver_cells[j]`` and ``_driver_positions[j]``,
      which remember where a driver is. A driver's entry lives in exactly one of
      these dicts, so per-driver updates serialise without a global map.

    Lock order is fixed and one-way: driver stripe first, then cell stripes in
    ascending index order. Nothing ever takes them the other way round, so the
    index cannot deadlock. ``nearby`` takes only cell stripes, one at a time, and
    holds none of them while it computes distances.
    """

    def __init__(self, cell_size_km: float = 1.0, reference_lat: float = 0.0, stripes: int = 16) -> None:
        if cell_size_km <= 0 or stripes <= 0:
            raise ValueError("cell size and stripe count must be positive")
        self.cell_size_km = cell_size_km
        self._stripes = stripes
        self._lat_step = cell_size_km / KM_PER_DEGREE
        self._lon_step = cell_size_km / (KM_PER_DEGREE * max(cos(radians(reference_lat)), 0.01))
        self._cell_locks = [threading.Lock() for _ in range(stripes)]
        self._cell_members: list[dict[Cell, set[str]]] = [{} for _ in range(stripes)]
        self._driver_locks = [threading.Lock() for _ in range(stripes)]
        self._driver_cells: list[dict[str, Cell]] = [{} for _ in range(stripes)]
        self._driver_positions: list[dict[str, Location]] = [{} for _ in range(stripes)]

    def cell_of(self, location: Location) -> Cell:
        return (floor(location.lon / self._lon_step), floor(location.lat / self._lat_step))

    def update(self, driver_id: str, location: Location) -> Cell:
        """Record a GPS ping. A ping inside the current cell costs one lock and no writes."""
        new_cell = self.cell_of(location)
        stripe = self._driver_stripe(driver_id)
        with self._driver_locks[stripe]:
            self._driver_positions[stripe][driver_id] = location
            cells = self._driver_cells[stripe]
            old_cell = cells.get(driver_id)
            if old_cell == new_cell:
                return new_cell  # the common case: still in the same square, no set edits
            cells[driver_id] = new_cell
            self._move(driver_id, old_cell, new_cell)
        return new_cell

    def position(self, driver_id: str) -> Location | None:
        """The authoritative last-known position. Matching reads it, nobody else writes it."""
        stripe = self._driver_stripe(driver_id)
        with self._driver_locks[stripe]:
            return self._driver_positions[stripe].get(driver_id)

    def remove(self, driver_id: str) -> None:
        """Driver went offline. Their id must not surface in any later search."""
        stripe = self._driver_stripe(driver_id)
        with self._driver_locks[stripe]:
            self._driver_positions[stripe].pop(driver_id, None)
            old_cell = self._driver_cells[stripe].pop(driver_id, None)
            if old_cell is not None:
                self._move(driver_id, old_cell, None)

    def nearby(self, centre: Location, radius_km: float) -> list[str]:
        """Cell ring covering the radius, then an exact distance filter.

        The ring is a superset: a 1 km grid searched at 1.5 km reads a 5x5 block
        of 25 cells and throws away the corners. That is the standard trade -- a
        cheap over-read followed by exact maths, rather than an exact index.
        """
        centre_cell = self.cell_of(centre)
        rings = ceil(radius_km / self.cell_size_km)
        found: list[str] = []
        for dx in range(-rings, rings + 1):
            for dy in range(-rings, rings + 1):
                found.extend(self._members((centre_cell[0] + dx, centre_cell[1] + dy)))
        inside = []
        for driver_id in found:
            position = self.position(driver_id)
            if position is not None and position.distance_km(centre) <= radius_km:
                inside.append(driver_id)
        return inside

    def cell_population(self, cell: Cell) -> int:
        return len(self._members(cell))

    def size(self) -> int:
        return sum(len(cells) for cells in self._driver_cells)

    # -- internals ---------------------------------------------------------------
    def _driver_stripe(self, driver_id: str) -> int:
        return zlib.crc32(driver_id.encode()) % self._stripes  # stable across processes

    def _cell_stripe(self, cell: Cell) -> int:
        return hash(cell) % self._stripes

    def _members(self, cell: Cell) -> list[str]:
        with self._cell_locks[self._cell_stripe(cell)]:
            return list(self._cell_members[self._cell_stripe(cell)].get(cell, ()))

    def _move(self, driver_id: str, old_cell: Cell | None, new_cell: Cell | None) -> None:
        """Caller holds the driver stripe. Cell stripes are taken in ascending order."""
        touched = {self._cell_stripe(c) for c in (old_cell, new_cell) if c is not None}
        acquired = [self._cell_locks[i] for i in sorted(touched)]
        for lock in acquired:
            lock.acquire()
        try:
            if old_cell is not None:
                members = self._cell_members[self._cell_stripe(old_cell)]
                bucket = members.get(old_cell)
                if bucket is not None:
                    bucket.discard(driver_id)
                    if not bucket:
                        del members[old_cell]
            if new_cell is not None:
                members = self._cell_members[self._cell_stripe(new_cell)]
                members.setdefault(new_cell, set()).add(driver_id)
        finally:
            for lock in reversed(acquired):
                lock.release()


# --8<-- [end:index]
