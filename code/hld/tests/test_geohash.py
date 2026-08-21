import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import NotFoundError, ValidationError
from hld.geohash import (
    BASE32,
    MAX_PRECISION,
    Direction,
    GeoIndex,
    adjacent,
    bounds,
    cell_size_km,
    cells_covering,
    decode,
    encode,
    haversine_km,
    neighbors,
    precision_for_radius_km,
)

SF = (37.7749, -122.4194)
NYC = (40.7128, -74.0060)


@pytest.mark.parametrize(
    ("lat", "lon", "precision", "expected"),
    [
        (37.7749, -122.4194, 5, "9q8yy"),
        (42.605, -5.603, 5, "ezs42"),
        (51.5074, -0.1278, 4, "gcpv"),
        (-33.8688, 151.2093, 4, "r3gx"),
        (0.0, 0.0, 8, "s0000000"),
        (90.0, 180.0, 3, "zzz"),
        (-90.0, -180.0, 3, "000"),
    ],
)
def test_encode_matches_reference_values(lat: float, lon: float, precision: int, expected: str) -> None:
    assert encode(lat, lon, precision) == expected
    assert encode(lat, lon, 12).startswith(expected)  # a prefix is a coarser cell


def test_decode_is_within_half_a_cell_and_bounds_contain_the_point() -> None:
    rng = random.Random(42)
    for _ in range(300):
        lat, lon = rng.uniform(-90, 90), rng.uniform(-180, 180)
        for precision in (1, 3, 6, 9, 12):
            geohash = encode(lat, lon, precision)
            box = bounds(geohash)
            assert box.contains(lat, lon)
            c_lat, c_lon = decode(geohash)
            assert abs(c_lat - lat) <= box.height_deg / 2 + 1e-9
            assert abs(c_lon - lon) <= box.width_deg / 2 + 1e-9
            assert encode(c_lat, c_lon, precision) == geohash


def test_every_first_character_is_its_own_cell() -> None:
    cells = {encode(*decode(ch), 1) for ch in BASE32}
    assert cells == set(BASE32)


def test_neighbors_are_adjacent_cells_and_handle_poles_and_the_antimeridian() -> None:
    cell = "9q8yy"
    box = bounds(cell)
    ring = neighbors(cell)
    assert len(ring) == len(set(ring)) == 8 and cell not in ring
    for other in ring:
        other_box = bounds(other)
        d_lat = abs(other_box.center[0] - box.center[0])
        d_lon = abs(other_box.center[1] - box.center[1])
        assert d_lat == pytest.approx(box.height_deg) or d_lat == pytest.approx(0)
        assert d_lon == pytest.approx(box.width_deg) or d_lon == pytest.approx(0)
    assert adjacent("9q", Direction.N) == "9r"  # as drawn in the figure
    assert adjacent("u", Direction.N) is None  # the top row has no northern neighbour
    assert len(neighbors("u")) == 5
    assert adjacent("8", Direction.W) == "x"  # longitude wraps at the antimeridian
    assert adjacent("x", Direction.E) == "8"


def test_boundary_problem_two_close_points_share_no_prefix() -> None:
    north, south = (45.0001, -122.5), (44.9999, -122.5)
    assert haversine_km(*north, *south) < 0.03
    assert encode(*north, 8)[0] != encode(*south, 8)[0]
    # ...and the neighbour step still finds the cell across the border
    assert encode(*south, 6) in neighbors(encode(*north, 6))


@pytest.mark.parametrize(
    ("precision", "width_km", "height_km"),
    [(1, 5004, 5004), (2, 1251, 625.5), (5, 4.89, 4.89), (6, 1.22, 0.611), (8, 0.0382, 0.0191)],
)
def test_cell_size_table(precision: int, width_km: float, height_km: float) -> None:
    width, height = cell_size_km(precision)
    assert width == pytest.approx(width_km, rel=0.01)
    assert height == pytest.approx(height_km, rel=0.01)
    at_60 = cell_size_km(precision, lat=60)
    assert at_60[0] == pytest.approx(width / 2, rel=1e-6) and at_60[1] == height


@pytest.mark.parametrize(
    ("radius_km", "precision"), [(0.015, 8), (0.1, 7), (0.5, 6), (1, 5), (5, 4), (50, 3), (2_000, 1)]
)
def test_precision_for_radius(radius_km: float, precision: int) -> None:
    assert precision_for_radius_km(radius_km) == precision
    assert min(cell_size_km(precision)) >= radius_km
    if precision < MAX_PRECISION:  # one level finer would no longer cover the radius
        assert min(cell_size_km(precision + 1)) < radius_km


def test_haversine() -> None:
    assert haversine_km(*SF, *NYC) == pytest.approx(4_130, rel=0.01)
    assert haversine_km(*SF, *SF) == 0
    assert haversine_km(0, 0, 0, 1) == pytest.approx(111.2, rel=0.01)
    assert haversine_km(0, 179.5, 0, -179.5) == pytest.approx(111.2, rel=0.01)


def test_cells_covering_one_ring_is_the_cell_plus_its_neighbours() -> None:
    cells = cells_covering(*SF, 1.0, 5)
    centre = encode(*SF, 5)
    assert len(cells) == 9 and centre in cells
    assert set(cells) == {centre, *neighbors(centre)}
    assert len(cells_covering(*SF, 3.0, 6)) == 11 * 11  # 3 km / 0.61 km tall cells -> 5 rings
    with pytest.raises(ValidationError):
        cells_covering(*SF, 50.0, 8)


def test_geo_index_nearby_matches_brute_force() -> None:
    rng = random.Random(7)
    points = {f"p{i}": (rng.uniform(37, 38), rng.uniform(-123, -121)) for i in range(2_000)}
    index = GeoIndex(precision=6)
    for item_id, (lat, lon) in points.items():
        index.add(item_id, lat, lon)
    assert len(index) == 2_000
    for _ in range(40):
        q_lat, q_lon = rng.uniform(37, 38), rng.uniform(-123, -121)
        radius = rng.uniform(0.3, 3.0)
        expected = {
            item_id
            for item_id, (lat, lon) in points.items()
            if haversine_km(q_lat, q_lon, lat, lon) <= radius
        }
        hits = index.nearby(q_lat, q_lon, radius, limit=10_000)
        assert {hit.item_id for hit in hits} == expected
        assert [hit.distance_km for hit in hits] == sorted(hit.distance_km for hit in hits)
        _, candidates = index.candidates(q_lat, q_lon, radius)
        assert len(expected) <= candidates < 2_000
    assert len(index.nearby(37.5, -122, 3.0, limit=3)) == 3


def test_geo_index_moves_and_removes_items() -> None:
    index = GeoIndex(precision=5)
    first = index.add("driver-1", *SF)
    assert index.nearby(*SF, 1.0) and len(index) == 1
    second = index.add("driver-1", *NYC)  # the driver moved: one entry, new cell
    assert first != second and len(index) == 1
    assert index.nearby(*SF, 1.0) == [] and index.nearby(*NYC, 1.0)[0].item_id == "driver-1"
    assert index.candidates(*SF, 1.0)[1] == 0  # the old cell was cleaned up
    index.remove("driver-1")
    assert len(index) == 0 and index.nearby(*NYC, 1.0) == []
    with pytest.raises(NotFoundError):
        index.remove("driver-1")


def test_validation_errors() -> None:
    for lat, lon in ((91, 0), (0, 181), (-91, 0), (0, -181)):
        with pytest.raises(ValidationError):
            encode(lat, lon)
        with pytest.raises(ValidationError):
            haversine_km(lat, lon, 0, 0)
    for precision in (0, MAX_PRECISION + 1):
        with pytest.raises(ValidationError):
            encode(0, 0, precision)
        with pytest.raises(ValidationError):
            cell_size_km(precision)
        with pytest.raises(ValidationError):
            GeoIndex(precision)
    for geohash in ("", "a", "9q8yA", "9" * 13):
        with pytest.raises(ValidationError):
            bounds(geohash)
    with pytest.raises(ValidationError):
        precision_for_radius_km(0)
    index = GeoIndex()
    with pytest.raises(ValidationError):
        index.add("", 0, 0)
    with pytest.raises(ValidationError):
        index.nearby(0, 0, 1.0, limit=0)
    with pytest.raises(ValidationError):
        index.nearby(0, 0, -1.0)


def test_concurrent_adds_and_moves() -> None:
    index = GeoIndex(precision=6)
    rng = random.Random(3)
    starts = [(rng.uniform(37, 38), rng.uniform(-123, -121)) for _ in range(400)]

    def work(i: int) -> int:
        lat, lon = starts[i]
        index.add(f"d{i}", lat, lon)
        index.add(f"d{i}", lat + 0.01, lon + 0.01)  # and then it moves
        return len(index.nearby(lat + 0.01, lon + 0.01, 0.2, limit=1_000))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(400)))
    assert all(n >= 1 for n in results)
    assert len(index) == 400
    cells, candidates = index.candidates(37.5, -122, 4.0)  # 4 km / 0.61 km tall cells -> 7 rings
    assert len(cells) == 15 * 15 and candidates <= 400
