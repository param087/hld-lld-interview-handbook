import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.quadtree import Point, QuadTree, Rect

WORLD = Rect(0, 0, 100, 100)


def seeded_tree(n: int = 60, seed: int = 42, capacity: int = 4) -> tuple[QuadTree, list[Point]]:
    rng = random.Random(seed)
    points = [Point(rng.uniform(0, 100), rng.uniform(0, 100), f"p{i}") for i in range(n)]
    tree = QuadTree(WORLD, capacity=capacity)
    for p in points:
        tree.insert(p)
    return tree, points


def test_reproduces_the_figure() -> None:
    tree, _ = seeded_tree()
    assert len(tree) == 60
    assert tree.stats()[0] == 49
    result = tree.query(Rect(60, 15, 90, 45))
    assert len(result.points) == 6
    assert result.nodes_visited == 10


def test_range_query_matches_brute_force() -> None:
    tree, points = seeded_tree(n=500, seed=1)
    rng = random.Random(2)
    for _ in range(50):
        x0, y0 = rng.uniform(0, 90), rng.uniform(0, 90)
        box = Rect(x0, y0, x0 + rng.uniform(1, 30), y0 + rng.uniform(1, 30))
        expected = {p for p in points if box.covers(p)}
        result = tree.query(box)
        assert set(result.points) == expected
        assert result.nodes_visited <= tree.stats()[0]
    everything = tree.query(WORLD)
    assert set(everything.points) == set(points)
    assert everything.nodes_visited == tree.stats()[0]


@pytest.mark.parametrize("k", [1, 3, 10, 600])
def test_nearest_matches_brute_force(k: int) -> None:
    tree, points = seeded_tree(n=500, seed=5)
    rng = random.Random(6)
    for _ in range(20):
        x, y = rng.uniform(-10, 110), rng.uniform(-10, 110)  # queries may fall outside the box
        expected = sorted(((p.x - x) ** 2 + (p.y - y) ** 2) ** 0.5 for p in points)[:k]
        got = tree.nearest(x, y, k=k)
        assert [d for d, _ in got] == pytest.approx(expected)
        assert len(got) == min(k, len(points))


def test_subdivision_is_adaptive_and_depth_is_capped() -> None:
    tree, _ = seeded_tree()
    nodes_before, depth_before = tree.stats()
    for i in range(200):
        tree.insert(Point(10 + i * 0.005, 10 + i * 0.005, f"c{i}"))
    nodes_after, depth_after = tree.stats()
    assert depth_after > depth_before and nodes_after > nodes_before
    far_away = tree.query(Rect(60, 15, 90, 45))
    assert far_away.nodes_visited == 10  # the other branches did not change
    capped = QuadTree(WORLD, capacity=2, max_depth=3)
    for _ in range(20):
        capped.insert(Point(50, 50, "dup"))  # identical coordinates cannot be separated
    assert len(capped) == 20 and capped.stats()[1] == 3
    assert len(capped.query(Rect(49, 49, 51, 51)).points) == 20


def test_validation_errors() -> None:
    with pytest.raises(ValidationError):
        Rect(1, 0, 0, 1)
    with pytest.raises(ValidationError):
        QuadTree(WORLD, capacity=0)
    with pytest.raises(ValidationError):
        QuadTree(WORLD, max_depth=0)
    tree = QuadTree(WORLD)
    with pytest.raises(ValidationError):
        tree.insert(Point(100, 50))  # the far edge is exclusive
    with pytest.raises(ValidationError):
        tree.insert(Point(-1, 50))
    with pytest.raises(ValidationError):
        tree.nearest(0, 0, k=0)
    assert tree.nearest(0, 0, k=3) == []
    assert tree.query(WORLD).points == []


def test_rect_geometry() -> None:
    box = Rect(0, 0, 10, 10)
    assert box.contains(Point(0, 0)) and not box.contains(Point(10, 10))
    assert box.covers(Point(10, 10))
    assert box.intersects(Rect(10, 10, 20, 20)) and not box.intersects(Rect(11, 0, 20, 10))
    assert box.distance_sq(5, 5) == 0
    assert box.distance_sq(13, 14) == 9 + 16


def test_concurrent_inserts_and_queries() -> None:
    tree = QuadTree(WORLD, capacity=4)

    def work(worker: int) -> int:
        rng = random.Random(worker)
        for i in range(250):
            tree.insert(Point(rng.uniform(0, 100), rng.uniform(0, 100), f"{worker}-{i}"))
        return len(tree.query(WORLD).points)

    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(work, range(8)))
    assert all(250 <= n <= 2_000 for n in counts)
    assert len(tree) == 2_000
    assert len(tree.query(WORLD).points) == 2_000
    assert len(tree.nearest(50, 50, k=5)) == 5
