from concurrent.futures import ThreadPoolExecutor

import pytest

from common import NotFoundError, ValidationError
from hld.geo_search import Business, Filters, GeoSearchIndex
from hld.geohash import bounds, encode, haversine_km

HERE = (37.7880, -122.4075)  # Union Square, San Francisco


def make(bid: str, lat: float, lon: float, **kwargs: object) -> Business:
    fields: dict = {
        "category": "coffee",
        "rating": 4.0,
        "review_count": 100,
        "price_tier": 2,
        **kwargs,
    }
    return Business(bid, f"place {bid}", lat, lon, **fields)


@pytest.fixture
def index() -> GeoSearchIndex:
    idx = GeoSearchIndex()
    idx.add(make("near", 37.7884, -122.4078))  # ~50 m
    idx.add(make("mid", 37.7955, -122.3937, rating=4.8, review_count=5000))  # ~1.6 km
    idx.add(make("far", 37.4419, -122.1430))  # Palo Alto, ~50 km
    return idx


def test_search_returns_only_places_inside_the_radius(index: GeoSearchIndex) -> None:
    page = index.search(*HERE, radius_km=1.0)
    assert [h.business.id for h in page.hits] == ["near"]
    assert page.stats.candidates >= page.stats.matched  # the exact filter did real work
    wider = index.search(*HERE, radius_km=5.0)
    assert {h.business.id for h in wider.hits} == {"near", "mid"}
    assert all(h.distance_km <= 5.0 for h in wider.hits)


def test_neighbour_scan_finds_a_place_across_the_cell_boundary(index: GeoSearchIndex) -> None:
    level = index.precision_for(1.0, HERE[0])
    box = bounds(encode(*HERE, level))
    across = make("across", box.lat_hi + 1e-5, HERE[1])
    assert encode(across.lat, across.lon, level) != encode(*HERE, level)  # different prefix
    assert haversine_km(*HERE, across.lat, across.lon) < 1.0  # but well inside the radius
    index.add(across)
    assert "across" in {h.business.id for h in index.search(*HERE, radius_km=1.0).hits}


@pytest.mark.parametrize("radius_km", [0.2, 1.0, 5.0, 40.0])
def test_precision_is_chosen_by_radius_and_never_rounded_up(radius_km: float) -> None:
    index = GeoSearchIndex()
    level = index.precision_for(radius_km, HERE[0])
    assert level in index.precisions
    from hld.geohash import cell_size_km

    width, height = cell_size_km(level, HERE[0])
    # A cell at least as wide as the radius means the 9-cell scan covers the whole circle.
    assert min(width, height) >= radius_km or level == index.precisions[0]


def test_filters_apply_after_the_geometry(index: GeoSearchIndex) -> None:
    index.add(make("pricey", 37.7885, -122.4079, category="sushi", rating=4.9, price_tier=4))
    strict = index.search(*HERE, radius_km=1.0, filters=Filters(category="coffee", min_rating=4.5))
    assert strict.hits == []  # "near" is coffee but only 4.0 stars
    cheap = index.search(*HERE, radius_km=1.0, filters=Filters(max_price_tier=3))
    assert [h.business.id for h in cheap.hits] == ["near"]


def test_ranking_blends_rating_proximity_and_popularity(index: GeoSearchIndex) -> None:
    index.add(make("great", 37.7890, -122.4080, rating=5.0, review_count=10_000))
    page = index.search(*HERE, radius_km=5.0)
    assert page.hits[0].business.id == "great"
    scores = [h.score for h in page.hits]
    assert scores == sorted(scores, reverse=True) and all(0.0 <= s <= 1.0 for s in scores)
    modest = make("x", *HERE, rating=3.0, review_count=10)
    strong = make("y", *HERE, rating=5.0, review_count=10)
    assert GeoSearchIndex.score(modest, 0.1, 5.0) > GeoSearchIndex.score(modest, 4.0, 5.0)
    assert GeoSearchIndex.score(strong, 1.0, 5.0) > GeoSearchIndex.score(modest, 1.0, 5.0)


def test_cache_serves_repeats_and_is_invalidated_by_writes(index: GeoSearchIndex) -> None:
    first = index.search(*HERE, radius_km=1.0)
    assert first.stats.cache_hits == 0
    second = index.search(*HERE, radius_km=1.0)
    assert second.stats.cache_hits == second.stats.cells_scanned == 9
    index.add(make("new", *HERE))
    third = index.search(*HERE, radius_km=1.0)
    assert third.stats.cache_hits == 8  # exactly one cell was invalidated at this precision
    assert "new" in {h.business.id for h in third.hits}


def test_validation_and_missing_ids(index: GeoSearchIndex) -> None:
    with pytest.raises(ValidationError):
        index.search(*HERE, radius_km=0)
    with pytest.raises(ValidationError):
        index.search(*HERE, radius_km=1.0, limit=0)
    with pytest.raises(ValidationError):
        make("bad", *HERE, rating=9.0)
    with pytest.raises(ValidationError):
        GeoSearchIndex(precisions=())
    with pytest.raises(NotFoundError):
        index.remove("ghost")


def test_concurrent_writes_and_searches_stay_consistent() -> None:
    index = GeoSearchIndex()

    def work(i: int) -> int:
        index.add(make(f"b{i}", 37.7880 + i * 0.00002, -122.4075))
        return index.search(*HERE, radius_km=1.0, limit=500).stats.matched

    with ThreadPoolExecutor(max_workers=8) as pool:
        matched = list(pool.map(work, range(200)))
    assert len(index) == 200
    assert index.search(*HERE, radius_km=1.0, limit=500).stats.matched == 200
    assert max(matched) <= 200 and min(matched) >= 1  # every search saw a consistent snapshot
