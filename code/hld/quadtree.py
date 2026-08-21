"""Point-region quadtree: insert, range query and k-nearest neighbours.

What the module demonstrates, in the order an interviewer asks about it:

* ``QuadTree`` splits a leaf into four quadrants when an insert would push it past ``capacity``,
  so dense areas get deep and empty areas stay shallow: the adaptivity a fixed geohash grid
  lacks (Manhattan and the Pacific get cells of the same size there).
* ``query`` descends only into nodes whose box intersects the search rectangle and reports how
  many it visited: 10 of 49 nodes for the figure on the page, instead of testing all 60 points.
* ``nearest`` is a best-first search over one heap keyed by distance: nodes by the distance to
  their box, points by their exact distance, so the k-th point popped is provably the k-th
  nearest. That is the "nearest K drivers" primitive of a ride-hailing dispatcher.
* ``_lock`` serialises inserts and queries, because location updates arrive from many threads.
"""

from __future__ import annotations

import heapq
import itertools
import random
import threading
from dataclasses import dataclass

from common import ValidationError


# --8<-- [start:geometry]
@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float
    item_id: str = ""


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned box. Node boxes are half-open (a point on the shared edge belongs to one
    child only); query boxes are closed, so a point on the query edge counts."""

    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValidationError("a Rect needs x0 < x1 and y0 < y1")

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def contains(self, p: Point) -> bool:
        return self.x0 <= p.x < self.x1 and self.y0 <= p.y < self.y1

    def covers(self, p: Point) -> bool:
        return self.x0 <= p.x <= self.x1 and self.y0 <= p.y <= self.y1

    def intersects(self, other: Rect) -> bool:
        return not (
            self.x0 > other.x1 or self.x1 < other.x0 or self.y0 > other.y1 or self.y1 < other.y0
        )

    def distance_sq(self, x: float, y: float) -> float:
        """Squared distance from a point to the nearest point of the box (0 inside it)."""
        dx = max(self.x0 - x, 0.0, x - self.x1)
        dy = max(self.y0 - y, 0.0, y - self.y1)
        return dx * dx + dy * dy


# --8<-- [end:geometry]


class _Node:
    __slots__ = ("box", "children", "depth", "points")

    def __init__(self, box: Rect, depth: int) -> None:
        self.box = box
        self.depth = depth
        self.points: list[Point] = []
        self.children: list[_Node] | None = None

    def subdivide(self) -> None:
        """Split into SW, SE, NW, NE quadrants and push the points down."""
        half_w, half_h = self.box.width / 2, self.box.height / 2
        self.children = [
            _Node(Rect(x, y, x + half_w, y + half_h), self.depth + 1)
            for y in (self.box.y0, self.box.y0 + half_h)
            for x in (self.box.x0, self.box.x0 + half_w)
        ]
        points, self.points = self.points, []
        for p in points:
            self.child_for(p).points.append(p)

    def child_for(self, p: Point) -> _Node:
        assert self.children is not None
        return next(child for child in self.children if child.box.contains(p))


# --8<-- [start:quadtree]
@dataclass(frozen=True, slots=True)
class RangeResult:
    points: list[Point]
    nodes_visited: int


class QuadTree:
    """Points in a square region, split adaptively; ``_lock`` guards the whole tree."""

    def __init__(self, boundary: Rect, capacity: int = 4, max_depth: int = 16) -> None:
        if capacity <= 0 or max_depth <= 0:
            raise ValidationError("capacity and max_depth must be positive")
        self._root = _Node(boundary, depth=0)
        self._capacity = capacity
        self._max_depth = max_depth
        self._size = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return self._size

    @property
    def boundary(self) -> Rect:
        return self._root.box

    def insert(self, p: Point) -> None:
        """Walk down to the leaf for ``p``; split it while it is full, unless ``max_depth`` is
        reached (which is what stops duplicate coordinates from splitting forever)."""
        if not self._root.box.contains(p):
            raise ValidationError(f"{p} is outside the tree boundary {self._root.box}")
        with self._lock:
            node = self._root
            while node.children is not None:
                node = node.child_for(p)
            while len(node.points) >= self._capacity and node.depth < self._max_depth:
                node.subdivide()
                node = node.child_for(p)
            node.points.append(p)
            self._size += 1

    def query(self, rect: Rect) -> RangeResult:
        """Every point inside ``rect``; only nodes whose box meets ``rect`` are visited."""
        found: list[Point] = []
        visited = 0
        with self._lock:
            stack = [self._root]
            while stack:
                node = stack.pop()
                if not node.box.intersects(rect):
                    continue
                visited += 1
                if node.children is None:
                    found.extend(p for p in node.points if rect.covers(p))
                else:
                    stack.extend(node.children)
        return RangeResult(found, visited)

    def nearest(self, x: float, y: float, k: int = 1) -> list[tuple[float, Point]]:
        """The ``k`` closest points as ``(distance, point)``, nearest first.

        One heap holds nodes (keyed by distance to their box) and points (exact distance). A
        point popped before any node is closer than anything those nodes could contain.
        """
        if k <= 0:
            raise ValidationError("k must be positive")
        tiebreak = itertools.count()
        heap: list[tuple[float, int, _Node | Point]] = []
        result: list[tuple[float, Point]] = []
        with self._lock:
            heapq.heappush(heap, (self._root.box.distance_sq(x, y), next(tiebreak), self._root))
            while heap and len(result) < k:
                dist_sq, _, item = heapq.heappop(heap)
                if isinstance(item, Point):
                    result.append((dist_sq**0.5, item))
                elif item.children is None:
                    for p in item.points:
                        d = (p.x - x) ** 2 + (p.y - y) ** 2
                        heapq.heappush(heap, (d, next(tiebreak), p))
                else:
                    for child in item.children:
                        heapq.heappush(heap, (child.box.distance_sq(x, y), next(tiebreak), child))
        return result

    def stats(self) -> tuple[int, int]:
        """``(node count, max depth)``."""
        with self._lock:
            count = depth = 0
            stack = [self._root]
            while stack:
                node = stack.pop()
                count += 1
                depth = max(depth, node.depth)
                stack.extend(node.children or [])
        return count, depth


# --8<-- [end:quadtree]


def main() -> None:
    rng = random.Random(42)
    tree = QuadTree(Rect(0, 0, 100, 100), capacity=4)
    for i in range(60):
        tree.insert(Point(rng.uniform(0, 100), rng.uniform(0, 100), f"p{i}"))
    nodes, depth = tree.stats()
    print(f"60 seeded points in a 100 x 100 box, capacity 4: {nodes} nodes, depth {depth}")
    box = Rect(60, 15, 90, 45)
    result = tree.query(box)
    print(
        f"range query x 60-90, y 15-45: {len(result.points)} points, "
        f"{result.nodes_visited} of {nodes} nodes visited (a scan would test all 60 points)"
    )
    print("  " + ", ".join(f"({p.x:.1f}, {p.y:.1f})" for p in sorted(result.points, key=lambda p: (p.x, p.y))))
    print("nearest 3 to (50, 50):")
    for distance, p in tree.nearest(50, 50, k=3):
        print(f"  {p.item_id:<4} ({p.x:.1f}, {p.y:.1f}) at {distance:.1f}")

    for i in range(200):
        tree.insert(Point(rng.uniform(9, 11), rng.uniform(9, 11), f"c{i}"))
    nodes_after, depth_after = tree.stats()
    print(
        f"200 more points inside a 2 x 2 patch around (10, 10): {nodes_after} nodes, depth {depth_after}; "
        "only that branch got deeper"
    )
    elsewhere = tree.query(box)
    patch = tree.query(Rect(9, 9, 11, 11))
    print(
        f"same range query elsewhere: {len(elsewhere.points)} points, {elsewhere.nodes_visited} nodes visited; "
        f"the patch itself: {len(patch.points)} points, {patch.nodes_visited} nodes visited"
    )
    print(f"nearest to (10, 10): {tree.nearest(10, 10, k=1)[0][1].item_id} (dense branch, still a heap walk)")


if __name__ == "__main__":
    main()
