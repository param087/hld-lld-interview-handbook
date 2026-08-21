"""Windowed top-K heavy hitters: a CMS plus min-heap per time bucket, merged across time and shards.

The crux of the trending-topics design in one module, built on ``hld.count_min_sketch`` so the
sketch itself is not reimplemented:

* One ``TopK`` (Count-Min Sketch + min-heap of candidates) per **time bucket**, so "trending in the
  last 5 minutes" and "top of the day" are answered by merging different sets of buckets rather
  than by keeping a separate counter for every window length.
* Sketches of the same shape merge by adding counters, which is what makes both merges cheap: over
  time (bucket + bucket) and over space (shard + shard).
* ``merge_top_k`` combines per-shard candidate lists into a global answer, and ``exact_top_k`` is
  the slow path you reconcile against when the answer has to be exactly right.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections import Counter
from collections.abc import Iterable, Sequence

from common import ValidationError
from hld.count_min_sketch import CountMinSketch, TopK, zipf_stream


# --8<-- [start:windowed]
class WindowedTopK:
    """Per-bucket sketches, merged on demand into any window the query asks for.

    ``_buckets`` maps a bucket index to its own ``TopK`` (sketch + candidate heap); ``_lock``
    guards that dictionary while each ``TopK`` guards its own state. Buckets older than
    ``retain`` are dropped, which is what bounds memory: a day of minutes, not a day of keys.
    """

    def __init__(
        self,
        k: int = 10,
        bucket_s: float = 60.0,
        retain: int = 1440,
        epsilon: float = 0.001,
        delta: float = 0.01,
    ) -> None:
        if k <= 0 or bucket_s <= 0 or retain <= 0:
            raise ValidationError("k, bucket_s and retain must be positive")
        self.k, self.bucket_s, self.retain = k, bucket_s, retain
        self.epsilon, self.delta = epsilon, delta
        self._buckets: dict[int, TopK] = {}
        self._newest: int | None = None
        self._lock = threading.Lock()

    def bucket_index(self, event_time: float) -> int:
        return int(event_time // self.bucket_s)

    def add(self, key: str, event_time: float, count: int = 1) -> None:
        """Route one event to its bucket. The sketch inside the bucket does the counting."""
        index = self.bucket_index(event_time)
        with self._lock:
            bucket = self._buckets.get(index)
            if bucket is None:
                bucket = self._buckets[index] = TopK(self.k, self.epsilon, self.delta)
            self._newest = index if self._newest is None else max(self._newest, index)
            self._evict()
        bucket.add(key, count)  # outside the dict lock: TopK has its own

    def _evict(self) -> None:
        """Drop buckets that fell out of the retention horizon; caller holds the lock."""
        if self._newest is None:
            return
        cutoff = self._newest - self.retain + 1
        for index in [i for i in self._buckets if i < cutoff]:
            del self._buckets[index]

    def _window(self, window_s: float | None) -> list[TopK]:
        with self._lock:
            if self._newest is None:
                return []
            if window_s is None:
                indexes = sorted(self._buckets)
            else:
                span = max(1, math.ceil(window_s / self.bucket_s))
                oldest = self._newest - span + 1
                indexes = sorted(i for i in self._buckets if i >= oldest)
            return [self._buckets[i] for i in indexes]

    def top(self, n: int, window_s: float | None = None) -> list[tuple[str, int]]:
        """Top-n over the last ``window_s`` seconds, or over everything retained.

        Merge the buckets' sketches into one, take the union of their heap candidates, and
        re-estimate each candidate against the merged sketch. A key that is heavy over the window
        is almost always heavy in at least one bucket, which is what makes the candidate union
        sufficient; the merged sketch is what makes the counts right.
        """
        if n <= 0:
            raise ValidationError("n must be positive")
        buckets = self._window(window_s)
        if not buckets:
            return []
        merged = CountMinSketch(self.epsilon, self.delta)
        candidates: set[str] = set()
        for bucket in buckets:
            merged.merge(bucket.sketch)
            candidates.update(key for key, _ in bucket.top(self.k))
        ranked = sorted(
            ((key, merged.estimate(key)) for key in candidates), key=lambda kv: (-kv[1], kv[0])
        )
        return ranked[:n]

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    @property
    def bucket_shape(self) -> tuple[int, int, int]:
        """``(width, depth, bytes)`` of every bucket's sketch - a function of epsilon and delta
        only, never of how many distinct keys the bucket sees."""
        probe = CountMinSketch(self.epsilon, self.delta)
        return probe.width, probe.depth, probe.memory_bytes()

    def memory_bytes(self) -> int:
        """Nominal sketch memory: buckets x counters x 4 B. Independent of the key count."""
        with self._lock:
            return sum(bucket.sketch.memory_bytes() for bucket in self._buckets.values())


# --8<-- [end:windowed]


# --8<-- [start:merge]
def shard_of(key: str, shards: int) -> int:
    """Which shard owns a key. Hash partitioning keeps a key on one shard, so the merge is a
    union rather than a sum - and a key that is hot everywhere still lands on one node."""
    if shards <= 0:
        raise ValidationError("shards must be positive")
    digest = hashlib.md5(key.encode(), usedforsecurity=False).digest()
    return int.from_bytes(digest[:4], "big") % shards


def merge_top_k(partials: Iterable[Sequence[tuple[str, int]]], n: int) -> list[tuple[str, int]]:
    """Scatter-gather: combine per-shard top-K lists into a global top-N.

    Counts are summed, so this is correct whether a key lives on one shard (hash partitioning) or
    on all of them (round-robin). It is approximate at the boundary: a key just below every
    shard's local cut-off can outrank one that made a single list, which is why K is a few times
    larger than the N you serve.
    """
    if n <= 0:
        raise ValidationError("n must be positive")
    totals: Counter[str] = Counter()
    for partial in partials:
        for key, count in partial:
            totals[key] += count
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def exact_top_k(stream: Iterable[str], n: int) -> list[tuple[str, int]]:
    """The slow path: a hash map. Exact, and O(distinct keys) in memory rather than O(1)."""
    if n <= 0:
        raise ValidationError("n must be positive")
    counts = Counter(stream)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


# --8<-- [end:merge]


def main() -> None:
    shards = 4
    minutes = 30
    events = 60_000
    stream = zipf_stream(keys=5_000, events=events, seed=42)
    base = 1_700_000_040.0

    fleet = [WindowedTopK(k=20, bucket_s=60.0, retain=1440) for _ in range(shards)]
    timeline: list[str] = []
    for i, key in enumerate(stream):
        event_time = base + (i * minutes * 60.0) / events  # spread evenly over 30 minutes
        fleet[shard_of(key, shards)].add(key, event_time)
        timeline.append(key)
    burst = 2_500  # a topic that only trends in the last 5 minutes
    for i in range(burst):
        event_time = base + (minutes - 5) * 60.0 + (i * 5 * 60.0) / burst
        fleet[shard_of("k_breaking", shards)].add("k_breaking", event_time)
        timeline.append("k_breaking")

    width, depth, per_bucket = fleet[0].bucket_shape
    buckets = sum(shard.bucket_count for shard in fleet)
    print(f"{shards} shards x {minutes} one-minute buckets; each bucket is a {width:,} x {depth} sketch ({per_bucket / 1024:.0f} KB)")
    total_mb = sum(shard.memory_bytes() for shard in fleet) / 1024 / 1024
    distinct = len(set(timeline))
    print(f"{buckets} buckets hold {total_mb:.1f} MB for {distinct:,} distinct keys and {len(timeline):,} events")

    whole = merge_top_k((shard.top(10) for shard in fleet), 5)
    exact = exact_top_k(timeline, 5)
    print("top-5 over the whole 30 minutes (merged across shards):")
    for (key, estimate), (_, truth) in zip(whole, exact, strict=True):
        print(f"  {key:<12} estimate={estimate:>6,}  exact={truth:>6,}")

    recent = [key for key, _ in merge_top_k((shard.top(10, window_s=300) for shard in fleet), 5)]
    print(f"top-5 over the last 5 minutes: {recent}")
    ranks = (recent.index("k_breaking") + 1, [key for key, _ in whole].index("k_breaking") + 1)
    print(f"  k_breaking ranks #{ranks[0]} over 5 minutes but only #{ranks[1]} over 30: that gap is 'trending'")

    approx_keys = [key for key, _ in merge_top_k((shard.top(20) for shard in fleet), 10)]
    exact_keys = [key for key, _ in exact_top_k(timeline, 10)]
    agree = sum(1 for key in approx_keys if key in exact_keys)
    bound = math.ceil(0.001 * len(timeline))
    print(f"approximate vs exact top-10: {agree}/10 keys agree; the sketch may overcount by at most eps*N = {bound}")


if __name__ == "__main__":
    main()
