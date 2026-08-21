"""Proximity search over a multi-precision geohash index: prefix + neighbours, then an exact filter.

The crux of the Yelp design in one module, built on ``hld.geohash`` so the proximity service and
the ride-hailing dispatcher share one partitioning scheme:

* Every business is indexed under its geohash prefix at *several* precisions at once, because a
  1 km search and a 20 km search want different cell sizes and neither should scan the other's.
* ``search`` picks the finest indexed precision whose cell is at least as wide as the radius, then
  reads the query cell **and its eight neighbours** - the boundary problem means a place 90 m away
  can sit in a cell that shares no prefix with yours.
* Candidates from those nine cells are filtered by exact haversine distance, then by business
  attributes, then ranked by a score that blends rating, proximity and popularity.
* ``_cell_cache`` stands in for the distributed cache in front of a sharded index: the cell key is
  stable and shared by every query in that neighbourhood, which is why this workload caches so well.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from common import NotFoundError, ValidationError
from hld.geohash import (
    MAX_PRECISION,
    bounds,
    encode,
    haversine_km,
    neighbors,
    precision_for_radius_km,
)

DEFAULT_PRECISIONS = (4, 5, 6, 7)  # ~40 km, ~5 km, ~1 km, ~150 m cells at the equator


# --8<-- [start:models]
@dataclass(frozen=True, slots=True)
class Business:
    """The subset of a business document the search path needs; the rest lives in the CRUD store."""

    id: str
    name: str
    lat: float
    lon: float
    category: str
    rating: float  # 0.0 - 5.0
    review_count: int
    price_tier: int  # 1 - 4

    def __post_init__(self) -> None:
        if not 0.0 <= self.rating <= 5.0:
            raise ValidationError("rating must be between 0 and 5")
        if self.review_count < 0 or not 1 <= self.price_tier <= 4:
            raise ValidationError("review_count must be >= 0 and price_tier must be 1-4")


@dataclass(frozen=True, slots=True)
class Filters:
    """Attribute filters applied *after* the geometry, never instead of it."""

    category: str | None = None
    min_rating: float = 0.0
    max_price_tier: int = 4

    def matches(self, business: Business) -> bool:
        return (
            (self.category is None or business.category == self.category)
            and business.rating >= self.min_rating
            and business.price_tier <= self.max_price_tier
        )


@dataclass(frozen=True, slots=True)
class Hit:
    business: Business
    distance_km: float
    score: float


@dataclass(frozen=True, slots=True)
class SearchStats:
    """What the query cost: the numbers you quote when the interviewer asks about fan-out."""

    precision: int
    cells_scanned: int
    candidates: int
    matched: int
    cache_hits: int


@dataclass(frozen=True, slots=True)
class SearchPage:
    hits: list[Hit]
    stats: SearchStats


# --8<-- [end:models]


# --8<-- [start:index]
class GeoSearchIndex:
    """Read-heavy proximity search over geohash prefixes at several precisions.

    ``_cells`` maps ``(precision, cell)`` to the business ids inside it - the posting lists a real
    deployment shards by cell prefix. ``_cell_cache`` is the cache in front of them, invalidated
    per cell on write. ``_lock`` guards all three; business writes are rare, searches are not.
    """

    def __init__(self, precisions: Sequence[int] = DEFAULT_PRECISIONS) -> None:
        levels = tuple(sorted(set(precisions)))
        if not levels or not all(1 <= p <= MAX_PRECISION for p in levels):
            raise ValidationError(f"precisions must be a non-empty subset of 1-{MAX_PRECISION}")
        self._precisions = levels
        self._cells: dict[tuple[int, str], set[str]] = {}
        self._businesses: dict[str, Business] = {}
        self._cell_cache: dict[tuple[int, str], frozenset[str]] = {}
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._businesses)

    @property
    def precisions(self) -> tuple[int, ...]:
        return self._precisions

    # -- writes ------------------------------------------------------------------------
    def add(self, business: Business) -> None:
        """Index a business under one prefix per precision. Writes are ~1000x rarer than reads."""
        with self._lock:
            self._unlink(business.id)
            self._businesses[business.id] = business
            for precision in self._precisions:
                key = (precision, encode(business.lat, business.lon, precision))
                self._cells.setdefault(key, set()).add(business.id)
                self._cell_cache.pop(key, None)  # invalidate exactly the cells that changed

    def remove(self, business_id: str) -> None:
        with self._lock:
            if business_id not in self._businesses:
                raise NotFoundError(f"unknown business {business_id!r}")
            self._unlink(business_id)

    def _unlink(self, business_id: str) -> None:
        """Detach a business from every level it is indexed in; caller holds the lock."""
        business = self._businesses.pop(business_id, None)
        if business is None:
            return
        for precision in self._precisions:
            key = (precision, encode(business.lat, business.lon, precision))
            members = self._cells.get(key)
            if members is None:
                continue
            members.discard(business_id)
            self._cell_cache.pop(key, None)
            if not members:
                del self._cells[key]

    # -- reads -------------------------------------------------------------------------
    def precision_for(self, radius_km: float, lat: float = 0.0) -> int:
        """The finest indexed precision whose cell is at least ``radius_km`` wide.

        Rounding *down* to a coarser level is always safe: a bigger cell plus its neighbours still
        covers the circle. Rounding up would silently miss results near the edge.
        """
        ideal = precision_for_radius_km(radius_km, lat)
        return max((p for p in self._precisions if p <= ideal), default=self._precisions[0])

    def search(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        filters: Filters | None = None,
        limit: int = 20,
    ) -> SearchPage:
        """Nine cells, an exact distance filter, attribute filters, then ranking."""
        if radius_km <= 0 or limit <= 0:
            raise ValidationError("radius_km and limit must be positive")
        rules = filters or Filters()
        precision = self.precision_for(radius_km, lat)
        centre = encode(lat, lon, precision)
        cells = [centre, *neighbors(centre)]  # the boundary problem is why the neighbours are read
        candidate_ids: set[str] = set()
        cache_hits = 0
        for cell in cells:
            ids, hit = self._posting_list(precision, cell)
            candidate_ids |= ids
            cache_hits += hit
        with self._lock:
            candidates = [self._businesses[b] for b in candidate_ids if b in self._businesses]
        hits = [
            Hit(business, distance, self.score(business, distance, radius_km))
            for business in candidates
            if (distance := haversine_km(lat, lon, business.lat, business.lon)) <= radius_km
            and rules.matches(business)
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.business.id))
        stats = SearchStats(precision, len(cells), len(candidates), len(hits), cache_hits)
        return SearchPage(hits[:limit], stats)

    def _posting_list(self, precision: int, cell: str) -> tuple[frozenset[str], int]:
        """Business ids in one cell, through the cache. Returns the ids and 1 on a cache hit."""
        key = (precision, cell)
        with self._lock:
            if (cached := self._cell_cache.get(key)) is not None:
                self._hits += 1
                return cached, 1
            self._misses += 1
            ids = frozenset(self._cells.get(key, ()))
            self._cell_cache[key] = ids
            return ids, 0

    @staticmethod
    def score(business: Business, distance_km: float, radius_km: float) -> float:
        """Rating, proximity and popularity, weighted and bounded to 0-1.

        Ranking is deliberately separate from geometry: the index answers "which places are near",
        the score answers "which of them to show first", and only the score changes weekly.
        """
        quality = business.rating / 5.0
        proximity = 1.0 - min(distance_km / radius_km, 1.0)
        popularity = min(math.log10(1 + business.review_count) / 3.0, 1.0)
        return round(0.5 * quality + 0.3 * proximity + 0.2 * popularity, 4)

    @property
    def cache_hit_rate(self) -> float:
        with self._lock:
            total = self._hits + self._misses
            return self._hits / total if total else 0.0


# --8<-- [end:index]


def _sample() -> list[Business]:
    """A dozen places around downtown San Francisco."""
    rows = [
        ("b1", "Blue Bottle Mint Plaza", 37.7827, -122.4089, "coffee", 4.5, 1800, 2),
        ("b2", "Sightglass Coffee", 37.7767, -122.4090, "coffee", 4.6, 3200, 2),
        ("b3", "Union Square Espresso", 37.7881, -122.4070, "coffee", 4.1, 90, 1),
        ("b4", "Powell Street Diner", 37.7845, -122.4079, "diner", 3.9, 640, 1),
        ("b5", "Ferry Building Oysters", 37.7955, -122.3937, "seafood", 4.7, 5200, 4),
        ("b6", "Chinatown Dumplings", 37.7941, -122.4078, "chinese", 4.4, 2100, 1),
        ("b7", "North Beach Trattoria", 37.8000, -122.4090, "italian", 4.2, 1500, 3),
        ("b8", "Coit Tower Cafe", 37.8024, -122.4058, "coffee", 3.8, 210, 2),
        ("b9", "Mission Taqueria", 37.7599, -122.4148, "mexican", 4.8, 9100, 1),
        ("b10", "Berkeley Campus Cafe", 37.8719, -122.2585, "coffee", 4.0, 340, 1),
        ("b11", "Oakland Jack London Grill", 37.7946, -122.2781, "grill", 4.3, 780, 2),
        ("b12", "Palo Alto Sushi", 37.4419, -122.1430, "japanese", 4.6, 1200, 3),
    ]
    return [Business(*row) for row in rows]


def main() -> None:
    index = GeoSearchIndex()
    for business in _sample():
        index.add(business)
    here = (37.7880, -122.4075)  # Union Square
    print(f"{len(index)} businesses indexed at precisions {', '.join(map(str, index.precisions))}")

    page = index.search(*here, radius_km=1.0)
    s = page.stats
    print(
        f"search 1 km: precision {s.precision}, {s.cells_scanned} cells (query + 8 neighbours), "
        f"{s.candidates} candidates of {len(index)}, {s.matched} inside the radius"
    )
    for hit in page.hits[:4]:
        b = hit.business
        print(f"  {hit.score:.3f}  {hit.distance_km:4.2f} km  {b.rating} stars  {b.name}")

    repeat = index.search(*here, radius_km=1.0)
    print(
        f"same search again: {repeat.stats.cache_hits}/{repeat.stats.cells_scanned} posting lists "
        f"served from cache, hit rate {index.cache_hit_rate:.0%}"
    )

    filtered = index.search(*here, radius_km=2.0, filters=Filters(category="coffee", min_rating=4.4))
    names = ", ".join(h.business.name for h in filtered.hits)
    print(f"filter category=coffee rating>=4.4 within 2 km: {filtered.stats.matched} -> {names}")

    wide = index.search(*here, radius_km=20.0)
    print(
        f"widen to 20 km: precision drops to {wide.stats.precision}, still {wide.stats.cells_scanned} "
        f"cells, {wide.stats.candidates} candidates, {wide.stats.matched} matches"
    )

    level = index.precision_for(1.0, here[0])
    box = bounds(encode(*here, level))
    edge = Business("b13", "Across the cell line", box.lat_hi + 1e-5, here[1], "coffee", 4.0, 50, 1)
    index.add(edge)
    found = any(h.business.id == "b13" for h in index.search(*here, radius_km=1.0).hits)
    print(
        f"boundary problem: b13 is {haversine_km(*here, edge.lat, edge.lon) * 1000:.0f} m north in cell "
        f"{encode(edge.lat, edge.lon, level)}, not {encode(*here, level)}; found by the neighbour scan: {found}"
    )

    index.remove("b3")
    after = index.search(*here, radius_km=1.0)
    print(
        f"remove b3: {after.stats.matched} matches, {after.stats.cache_hits}/{after.stats.cells_scanned} "
        "cache hits - only the cells it lived in were invalidated"
    )


if __name__ == "__main__":
    main()
