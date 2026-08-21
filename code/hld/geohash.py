"""Geohash: encode, decode, cell bounds, neighbours, the precision table and the proximity query.

What the module demonstrates, in the order an interviewer asks about it:

* ``encode`` bisects longitude and latitude alternately (longitude first) and packs five bits per
  base32 character, so every extra character splits the cell 32 ways and a prefix *is* a cell:
  points that share a prefix are near each other. ``bounds`` and ``decode`` reverse it.
* The converse is false: ``adjacent`` and ``neighbors`` exist because two points a few metres
  apart can sit in cells that share no prefix at all (the boundary problem), so every proximity
  query reads the query cell *and* its eight neighbours.
* ``cell_size_km`` derives the precision table instead of memorising it; ``precision_for_radius_km``
  turns a search radius into a prefix length; ``cells_covering`` returns the cells a radius needs.
* ``GeoIndex`` is the query pattern itself: candidates from the covering cells, an exact haversine
  filter, a sort by distance. The Uber and Yelp case studies build on it.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from common import NotFoundError, ValidationError

BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
MAX_PRECISION = 12
EARTH_RADIUS_KM = 6371.0
KM_PER_DEGREE = math.pi * EARTH_RADIUS_KM / 180  # one degree of latitude, ~111 km
MAX_RINGS = 8  # a covering of (2 x 8 + 1)^2 = 289 cells is the most a single query may scan

_DECODE = MappingProxyType({ch: i for i, ch in enumerate(BASE32)})  # read-only lookup table


def _validate_point(lat: float, lon: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValidationError(f"({lat}, {lon}) is not a valid latitude/longitude pair")


def _validate_hash(geohash: str) -> None:
    if not 1 <= len(geohash) <= MAX_PRECISION or any(ch not in _DECODE for ch in geohash):
        raise ValidationError(f"{geohash!r} is not a geohash of 1-{MAX_PRECISION} base32 characters")


# --8<-- [start:encode]
@dataclass(frozen=True, slots=True)
class Bounds:
    """The rectangle a geohash names, in degrees."""

    lat_lo: float
    lat_hi: float
    lon_lo: float
    lon_hi: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.lat_lo + self.lat_hi) / 2, (self.lon_lo + self.lon_hi) / 2

    @property
    def height_deg(self) -> float:
        return self.lat_hi - self.lat_lo

    @property
    def width_deg(self) -> float:
        return self.lon_hi - self.lon_lo

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_lo <= lat < self.lat_hi and self.lon_lo <= lon < self.lon_hi


def encode(lat: float, lon: float, precision: int = 9) -> str:
    """Geohash of a point: bisect longitude, then latitude, then longitude... and emit five bits
    per base32 character. ``precision`` characters carry 5 x precision bits."""
    _validate_point(lat, lon)
    if not 1 <= precision <= MAX_PRECISION:
        raise ValidationError(f"precision must be 1-{MAX_PRECISION}")
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    chars: list[str] = []
    bits = bit_count = 0
    longitude_turn = True
    while len(chars) < precision:
        if longitude_turn:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                bits, lon_lo = bits * 2 + 1, mid
            else:
                bits, lon_hi = bits * 2, mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                bits, lat_lo = bits * 2 + 1, mid
            else:
                bits, lat_hi = bits * 2, mid
        longitude_turn = not longitude_turn
        bit_count += 1
        if bit_count == 5:
            chars.append(BASE32[bits])
            bits = bit_count = 0
    return "".join(chars)


def bounds(geohash: str) -> Bounds:
    """Replay the bisections a geohash encodes to recover its cell."""
    _validate_hash(geohash)
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    longitude_turn = True
    for ch in geohash:
        value = _DECODE[ch]
        for shift in (4, 3, 2, 1, 0):
            bit = (value >> shift) & 1
            if longitude_turn:
                mid = (lon_lo + lon_hi) / 2
                lon_lo, lon_hi = (mid, lon_hi) if bit else (lon_lo, mid)
            else:
                mid = (lat_lo + lat_hi) / 2
                lat_lo, lat_hi = (mid, lat_hi) if bit else (lat_lo, mid)
            longitude_turn = not longitude_turn
    return Bounds(lat_lo, lat_hi, lon_lo, lon_hi)


def decode(geohash: str) -> tuple[float, float]:
    """Centre of the cell: the best point estimate, off by at most half a cell in each axis."""
    return bounds(geohash).center


# --8<-- [end:encode]


# --8<-- [start:neighbors]
class Direction(Enum):
    """Steps in (latitude, longitude) cell units, clockwise from north."""

    N = (1, 0)
    NE = (1, 1)
    E = (0, 1)
    SE = (-1, 1)
    S = (-1, 0)
    SW = (-1, -1)
    W = (0, -1)
    NW = (1, -1)


def adjacent(geohash: str, direction: Direction) -> str | None:
    """The cell one step away, or ``None`` beyond a pole.

    Every cell at a given precision has the same size in degrees, so stepping from the centre
    by one cell height or width and re-encoding is exact and sidesteps the base32 irregularity.
    Longitude wraps at the antimeridian.
    """
    box = bounds(geohash)
    d_lat, d_lon = direction.value
    lat = box.center[0] + d_lat * box.height_deg
    lon = box.center[1] + d_lon * box.width_deg
    if not -90 <= lat <= 90:
        return None
    if lon >= 180:
        lon -= 360
    elif lon < -180:
        lon += 360
    return encode(lat, lon, len(geohash))


def neighbors(geohash: str) -> list[str]:
    """The surrounding cells clockwise from north: eight, fewer at a pole."""
    return [cell for d in Direction if (cell := adjacent(geohash, d)) is not None]


def cell_size_km(precision: int, lat: float = 0.0) -> tuple[float, float]:
    """``(width, height)`` of a cell in km at latitude ``lat``.

    5 x precision bits split longitude-first, so longitude gets ceil(5p/2) bits and latitude
    floor(5p/2): odd precisions give square cells, even ones are twice as wide as tall. Width
    shrinks by cos(lat) away from the equator; height does not.
    """
    if not 1 <= precision <= MAX_PRECISION:
        raise ValidationError(f"precision must be 1-{MAX_PRECISION}")
    lon_bits = (5 * precision + 1) // 2
    lat_bits = 5 * precision // 2
    width = 2 * math.pi * EARTH_RADIUS_KM / 2**lon_bits * math.cos(math.radians(lat))
    height = math.pi * EARTH_RADIUS_KM / 2**lat_bits
    return width, height


def precision_for_radius_km(radius_km: float, lat: float = 0.0) -> int:
    """The finest precision whose cell is still at least ``radius_km`` in both directions, so
    the query cell plus its eight neighbours contain every point within the radius."""
    if radius_km <= 0:
        raise ValidationError("radius must be positive")
    for precision in range(MAX_PRECISION, 1, -1):
        if min(cell_size_km(precision, lat)) >= radius_km:
            return precision
    return 1


def cells_covering(lat: float, lon: float, radius_km: float, precision: int) -> list[str]:
    """Cells at ``precision`` whose union contains every point within ``radius_km``: the query
    cell plus ``rings`` rings around it, where one ring is the classic nine-cell search."""
    _validate_point(lat, lon)
    if radius_km <= 0:
        raise ValidationError("radius must be positive")
    far_lat = min(90.0, abs(lat) + radius_km / KM_PER_DEGREE)  # narrowest cells the circle touches
    rings = max(1, math.ceil(radius_km / min(cell_size_km(precision, far_lat))))
    if rings > MAX_RINGS:
        raise ValidationError(
            f"a {radius_km} km radius needs {rings} rings of precision-{precision} cells; "
            "index at a coarser precision"
        )
    box = bounds(encode(lat, lon, precision))
    c_lat, c_lon = box.center
    cells: list[str] = []
    for d_lat in range(-rings, rings + 1):
        step_lat = c_lat + d_lat * box.height_deg
        if not -90 <= step_lat <= 90:
            continue
        for d_lon in range(-rings, rings + 1):
            step_lon = (c_lon + d_lon * box.width_deg + 180) % 360 - 180
            cell = encode(step_lat, step_lon, precision)
            if cell not in cells:
                cells.append(cell)
    return cells


# --8<-- [end:neighbors]


# --8<-- [start:haversine]
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Euclidean distance on degrees is wrong twice over: a degree of
    longitude shrinks with cos(latitude), and the surface curves."""
    _validate_point(lat1, lon1)
    _validate_point(lat2, lon2)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# --8<-- [end:haversine]


# --8<-- [start:index]
@dataclass(frozen=True, slots=True)
class Hit:
    item_id: str
    lat: float
    lon: float
    distance_km: float


class GeoIndex:
    """Proximity search over geohash cells at one fixed precision.

    ``_cells`` maps a cell to the ids inside it and ``_points`` maps an id to its coordinates and
    cell; ``_lock`` guards both, because items move (drivers, couriers) while queries run. The
    query pattern: covering cells, candidate ids, exact haversine filter, sort by distance.
    """

    def __init__(self, precision: int = 6) -> None:
        if not 1 <= precision <= MAX_PRECISION:
            raise ValidationError(f"precision must be 1-{MAX_PRECISION}")
        self._precision = precision
        self._cells: dict[str, set[str]] = {}
        self._points: dict[str, tuple[float, float, str]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._points)

    @property
    def precision(self) -> int:
        return self._precision

    def add(self, item_id: str, lat: float, lon: float) -> str:
        """Insert an item or move it; returns the cell it now lives in."""
        if not item_id:
            raise ValidationError("item_id must be non-empty")
        _validate_point(lat, lon)
        cell = encode(lat, lon, self._precision)
        with self._lock:
            self._unlink(item_id)
            self._points[item_id] = (lat, lon, cell)
            self._cells.setdefault(cell, set()).add(item_id)
        return cell

    def remove(self, item_id: str) -> None:
        with self._lock:
            if item_id not in self._points:
                raise NotFoundError(f"unknown item {item_id!r}")
            self._unlink(item_id)

    def _unlink(self, item_id: str) -> None:
        """Detach an item from its cell; caller holds the lock."""
        previous = self._points.pop(item_id, None)
        if previous is None:
            return
        members = self._cells[previous[2]]
        members.discard(item_id)
        if not members:
            del self._cells[previous[2]]

    def nearby(self, lat: float, lon: float, radius_km: float, limit: int = 10) -> list[Hit]:
        """Items within ``radius_km``, nearest first, at most ``limit`` of them."""
        if limit <= 0:
            raise ValidationError("limit must be positive")
        cells = cells_covering(lat, lon, radius_km, self._precision)
        with self._lock:
            candidates = [
                (item_id, *self._points[item_id][:2])
                for cell in cells
                for item_id in self._cells.get(cell, ())
            ]
        hits = [
            Hit(item_id, p_lat, p_lon, distance)
            for item_id, p_lat, p_lon in candidates
            if (distance := haversine_km(lat, lon, p_lat, p_lon)) <= radius_km
        ]
        hits.sort(key=lambda hit: (hit.distance_km, hit.item_id))
        return hits[:limit]

    def candidates(self, lat: float, lon: float, radius_km: float) -> tuple[list[str], int]:
        """``(covering cells, candidate count)`` for a query: the cost before the exact filter."""
        cells = cells_covering(lat, lon, radius_km, self._precision)
        with self._lock:
            count = sum(len(self._cells.get(cell, ())) for cell in cells)
        return cells, count


# --8<-- [end:index]


def main() -> None:
    sf = (37.7749, -122.4194)
    print(
        "encode(37.7749, -122.4194) by precision: "
        + " | ".join(encode(*sf, precision) for precision in range(1, 9))
    )
    lat, lon = decode("9q8yy")
    width, height = cell_size_km(5, lat)
    print(
        f"decode('9q8yy') = ({lat:.4f}, {lon:.4f}); the cell is {width:.1f} km x {height:.1f} km, "
        "so the error is at most half of that"
    )
    print("precision table, width x height at the equator (width shrinks by cos(lat): x0.79 at SF):")

    def fmt(km: float) -> str:
        return f"{km:,.0f} km" if km >= 100 else f"{km:.2g} km" if km >= 1 else f"{km * 1000:.3g} m"

    rows = [f"{p}: {fmt(cell_size_km(p)[0])} x {fmt(cell_size_km(p)[1])}" for p in range(1, 9)]
    for left, right in zip(rows[:4], rows[4:], strict=True):
        print(f"  {left:<26} {right}")
    ring = ", ".join(f"{d.name}={adjacent('9q8yy', d)}" for d in Direction)
    print(f"neighbours of 9q8yy: {ring}")
    north, south = (45.0001, -122.5), (44.9999, -122.5)
    print(
        f"boundary problem: {encode(*north, 6)} and {encode(*south, 6)} are "
        f"{haversine_km(*north, *south) * 1000:.0f} m apart and share no prefix"
    )

    radius_km = 2.0
    precision = precision_for_radius_km(radius_km, lat=37.8)
    index = GeoIndex(precision)
    places = {
        "Powell St station": (37.7844, -122.4078),
        "Ferry Building": (37.7955, -122.3937),
        "Coit Tower": (37.8024, -122.4058),
        "Golden Gate Bridge": (37.8199, -122.4783),
        "Oakland, Jack London Sq": (37.7946, -122.2781),
        "Berkeley campus": (37.8719, -122.2585),
        "Palo Alto": (37.4419, -122.1430),
    }
    for name, (p_lat, p_lon) in places.items():
        index.add(name, p_lat, p_lon)
    union_square = (37.7880, -122.4075)
    cells, candidates = index.candidates(*union_square, radius_km)
    print(
        f"search {radius_km:g} km around Union Square: precision {precision} "
        f"({cell_size_km(precision, 37.8)[0]:.1f} km x {cell_size_km(precision)[1]:.1f} km cells), "
        f"{len(cells)} cells, {candidates} of {len(index)} places are candidates"
    )
    for hit in index.nearby(*union_square, radius_km):
        print(f"  {hit.distance_km:4.1f} km  {hit.item_id}")
    skipped = [
        f"{name} ({haversine_km(*union_square, *coords):.0f} km)"
        for name, coords in places.items()
        if encode(*coords, precision) not in cells
    ]
    print(f"  never read (outside the {len(cells)} cells): {', '.join(skipped)}")


if __name__ == "__main__":
    main()
