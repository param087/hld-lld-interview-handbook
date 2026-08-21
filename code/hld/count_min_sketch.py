"""Count-Min Sketch and the CMS-plus-heap top-K: frequencies in sub-linear memory.

A sketch is ``d`` rows of ``w`` counters. ``add`` increments one counter per row (each row
hashes differently) and ``estimate`` returns the minimum of those counters. Collisions can
only add, so an estimate never undercounts, and it overcounts by at most ``eps * N`` (``N``
is the total count) with probability ``1 - delta`` when ``w = ceil(e / eps)`` and
``d = ceil(ln(1 / delta))``. ``TopK`` pairs a sketch with a min-heap of the ``k`` largest
estimates: trending topics and heavy-hitter detection without a counter per key.
"""

from __future__ import annotations

import hashlib
import heapq
import math
import random
import threading
from collections import Counter

from common import ValidationError


# --8<-- [start:sketch]
class CountMinSketch:
    """``depth`` rows of ``width`` counters. ``_lock`` guards the rows and ``total``; reads
    of a single counter are atomic, so ``estimate`` runs without it and may lag an in-flight
    ``add`` by one increment, which only makes the estimate smaller, never wrong."""

    def __init__(self, epsilon: float = 0.001, delta: float = 0.01) -> None:
        if not 0.0 < epsilon < 1.0 or not 0.0 < delta < 1.0:
            raise ValidationError("epsilon and delta must be in (0, 1)")
        self.epsilon, self.delta = epsilon, delta
        self.width = math.ceil(math.e / epsilon)
        self.depth = math.ceil(math.log(1.0 / delta))
        self._rows = [[0] * self.width for _ in range(self.depth)]
        self.total = 0
        self._lock = threading.Lock()

    def _slots(self, item: str) -> list[int]:
        """One counter index per row, by double hashing ``h1 + row * h2 (mod width)``."""
        digest = hashlib.md5(item.encode(), usedforsecurity=False).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big") | 1
        return [(h1 + row * h2) % self.width for row in range(self.depth)]

    def add(self, item: str, count: int = 1) -> None:
        if count <= 0:
            raise ValidationError("count must be positive")
        slots = self._slots(item)
        with self._lock:
            for row, slot in zip(self._rows, slots, strict=True):
                row[slot] += count
            self.total += count

    def estimate(self, item: str) -> int:
        """Never below the true count; above it by at most ``error_bound`` w.p. 1 - delta."""
        return min(row[slot] for row, slot in zip(self._rows, self._slots(item), strict=True))

    @property
    def error_bound(self) -> int:
        """``eps * N``: the overcount you should expect at most, for the current total."""
        return math.ceil(self.epsilon * self.total)

    def memory_bytes(self) -> int:
        """Nominal size with 4-byte counters (Python ints are bigger; production is not)."""
        return 4 * self.width * self.depth

    def merge(self, other: CountMinSketch) -> None:
        """Add another sketch of identical shape counter by counter (shards to a global view)."""
        if other is self:
            raise ValidationError("cannot merge a sketch into itself")
        if (other.width, other.depth) != (self.width, self.depth):
            raise ValidationError("sketches must have the same width and depth")
        first, second = sorted((self, other), key=id)  # fixed lock order, no deadlock
        with first._lock, second._lock:
            for mine, theirs in zip(self._rows, other._rows, strict=True):
                for slot, count in enumerate(theirs):
                    mine[slot] += count
            self.total += other.total


# --8<-- [end:sketch]
# --8<-- [start:topk]
class TopK:
    """Heavy hitters: a sketch for counts and a min-heap of ``k`` candidates.

    ``_members`` maps each candidate to its current estimate; the heap holds ``(estimate,
    item)`` pairs and may contain stale entries (an item's older, smaller estimate, or an
    evicted item), which ``_drop_stale`` pops before the heap's minimum is trusted. A new item
    enters when its estimate beats that minimum, evicting it. ``_lock`` guards the heap and
    ``_members``; the sketch has its own lock.
    """

    def __init__(self, k: int, epsilon: float = 0.001, delta: float = 0.01) -> None:
        if k <= 0:
            raise ValidationError("k must be positive")
        self.k = k
        self.sketch = CountMinSketch(epsilon, delta)
        self._heap: list[tuple[int, str]] = []
        self._members: dict[str, int] = {}
        self._lock = threading.Lock()

    def _drop_stale(self) -> None:
        heap = self._heap
        while heap and self._members.get(heap[0][1]) != heap[0][0]:
            heapq.heappop(heap)

    def add(self, item: str, count: int = 1) -> None:
        self.sketch.add(item, count)
        estimate = self.sketch.estimate(item)
        with self._lock:
            if item in self._members:
                self._members[item] = estimate
                heapq.heappush(self._heap, (estimate, item))  # the older entry is now stale
            else:
                self._drop_stale()
                if len(self._members) >= self.k:
                    if estimate <= self._heap[0][0]:
                        return
                    _, evicted = heapq.heappop(self._heap)
                    del self._members[evicted]
                self._members[item] = estimate
                heapq.heappush(self._heap, (estimate, item))
            if len(self._heap) > 4 * self.k:  # bound the stale entries
                self._heap = [(est, key) for key, est in self._members.items()]
                heapq.heapify(self._heap)

    def top(self, n: int | None = None) -> list[tuple[str, int]]:
        """Candidates by estimated count, largest first (ties by item for determinism)."""
        with self._lock:
            ranked = sorted(self._members.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[: n or self.k]


# --8<-- [end:topk]


def zipf_stream(keys: int, events: int, seed: int = 42, exponent: float = 1.1) -> list[str]:
    """A skewed stream: key ``i`` is drawn with weight ``1 / (i + 1) ^ exponent``."""
    rng = random.Random(seed)
    population = [f"k{i}" for i in range(keys)]
    weights = [1.0 / (i + 1) ** exponent for i in range(keys)]
    return rng.choices(population, weights, k=events)


def main() -> None:
    stream = zipf_stream(keys=10_000, events=50_000)
    exact = Counter(stream)
    topk = TopK(k=5, epsilon=0.001, delta=0.01)
    for item in stream:
        topk.add(item)
    sketch = topk.sketch
    print(
        f"sketch: eps={sketch.epsilon}, delta={sketch.delta} -> width={sketch.width:,} x "
        f"depth={sketch.depth} = {sketch.width * sketch.depth:,} counters "
        f"({sketch.memory_bytes() / 1024:.0f} KB) for {len(exact):,} distinct keys"
    )
    print(f"N={sketch.total:,} events, error bound eps*N={sketch.error_bound}")
    for item, estimate in topk.top():
        print(f"  {item:<6} estimate={estimate:>6,} exact={exact[item]:>6,}")
    overs = [sketch.estimate(item) - count for item, count in exact.items()]
    beyond = sum(over > sketch.error_bound for over in overs)
    print(
        f"overestimate over all keys: max={max(overs)}, mean={sum(overs) / len(overs):.2f}, "
        f"never negative={min(overs) >= 0}, beyond the bound: {beyond} "
        f"(allowed: {sketch.delta:.0%} of keys = {int(sketch.delta * len(exact))})"
    )
    exact_top = [k for k, _ in sorted(exact.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    print(f"top-5 by sketch == top-5 exact: {[k for k, _ in topk.top()] == exact_top}")


if __name__ == "__main__":
    main()
