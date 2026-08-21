"""Latency percentiles from bucketed histograms, and two ways averages lie.

What the module demonstrates, in the order an interviewer asks about it:

* ``exact_percentile`` is the nearest-rank percentile over raw samples: the reference answer,
  and the one you cannot afford in production because it needs every sample, sorted.
* ``Histogram`` is what a metrics pipeline ships instead: a fixed set of exponential buckets
  with a count each. ``percentile`` interpolates inside the bucket the rank falls into, so the
  error is bounded by the bucket width however many samples arrive.
* ``Histogram.merge`` adds bucket counts. It is the only correct way to turn per-host latencies
  into a fleet percentile; ``mean_of_percentiles`` is the tempting wrong way, and the demo
  measures how far off it is.
* A bimodal workload (cache hits and misses) shows the mean describing a latency no request
  has, while p50 and p99 describe both modes.
"""

from __future__ import annotations

import bisect
import functools
import itertools
import math
import random
import statistics
import threading
from collections.abc import Iterable, Sequence

from common import ValidationError


# --8<-- [start:exact]
def exact_percentile(samples: Sequence[float], q: float) -> float:
    """Nearest-rank percentile: the value at rank ``ceil(q/100 * n)`` of the sorted samples.

    Exact, but it needs every sample in memory and a sort, which is why production systems
    ship bucket counts instead and accept a bounded estimation error.
    """
    if not samples:
        raise ValidationError("cannot take a percentile of no samples")
    if not 0 < q <= 100:
        raise ValidationError("q must be in (0, 100]")
    ordered = sorted(samples)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


# --8<-- [end:exact]


# --8<-- [start:histogram]
class Histogram:
    """Bucketed latency histogram: ``bounds[i]`` is the inclusive upper edge of bucket ``i``,
    and one overflow bucket catches everything above the last edge.

    ``_lock`` guards ``_counts``, ``_total`` and ``_sum``: request threads call ``observe``
    concurrently, and readers take a snapshot under the lock and compute outside it.
    """

    def __init__(self, bounds: Sequence[float]) -> None:
        edges = tuple(float(b) for b in bounds)
        if not edges or edges[0] <= 0 or any(b <= a for a, b in itertools.pairwise(edges)):
            raise ValidationError("bounds must be positive and strictly increasing")
        self._bounds = edges
        self._counts = [0] * (len(edges) + 1)
        self._total = 0
        self._sum = 0.0
        self._lock = threading.Lock()

    @classmethod
    def exponential(cls, start: float, factor: float, count: int) -> Histogram:
        """``count`` edges growing by ``factor``: start, start*factor, start*factor^2, ...

        Any percentile estimate lands in the same bucket as the exact value, so ``factor`` is
        the worst-case relative error: 1.25 means 25 %, and 40 such buckets span 1 ms to ~6 s.
        """
        if start <= 0 or factor <= 1 or count <= 0:
            raise ValidationError("need start > 0, factor > 1 and count > 0")
        return cls([start * factor**i for i in range(count)])

    @property
    def bounds(self) -> tuple[float, ...]:
        return self._bounds

    @property
    def count(self) -> int:
        with self._lock:
            return self._total

    @property
    def mean(self) -> float:
        with self._lock:
            return self._sum / self._total if self._total else 0.0

    def observe(self, value: float) -> None:
        """Count one sample: a binary search over the edges, a few bytes however many samples."""
        if math.isnan(value) or value < 0:
            raise ValidationError("latency samples must be non-negative numbers")
        idx = bisect.bisect_left(self._bounds, value)  # len(bounds) is the overflow bucket
        with self._lock:
            self._counts[idx] += 1
            self._total += 1
            self._sum += value

    def snapshot(self) -> tuple[list[int], int, float]:
        """``(bucket counts, total count, sum)`` taken atomically."""
        with self._lock:
            return list(self._counts), self._total, self._sum

    def merge(self, other: Histogram) -> Histogram:
        """Fleet view of per-host histograms: bucket counts add. Needs identical edges."""
        if other.bounds != self._bounds:
            raise ValidationError("cannot merge histograms with different bucket edges")
        mine, my_total, my_sum = self.snapshot()
        theirs, their_total, their_sum = other.snapshot()
        merged = Histogram(self._bounds)
        merged._counts = [a + b for a, b in zip(mine, theirs, strict=True)]
        merged._total = my_total + their_total
        merged._sum = my_sum + their_sum
        return merged

    def percentile(self, q: float) -> float:
        """Estimate percentile ``q``: walk the buckets to the one holding the rank, then
        interpolate linearly inside it (what Prometheus's ``histogram_quantile`` does).

        The overflow bucket only says "above the last edge", so that edge is returned for it.
        """
        if not 0 < q <= 100:
            raise ValidationError("q must be in (0, 100]")
        counts, total, _ = self.snapshot()
        if total == 0:
            raise ValidationError("cannot take a percentile of an empty histogram")
        rank = max(1, math.ceil(q / 100 * total))
        below = 0
        for idx, n in enumerate(counts):
            if below + n >= rank:
                if idx == len(self._bounds):
                    return self._bounds[-1]
                lo = self._bounds[idx - 1] if idx else 0.0
                return lo + (self._bounds[idx] - lo) * (rank - below) / n
            below += n
        raise AssertionError("rank beyond the total count; unreachable")


# --8<-- [end:histogram]


# --8<-- [start:fleet]
def mean_of_percentiles(histograms: Iterable[Histogram], q: float) -> float:
    """The tempting wrong answer: average each host's percentile.

    Percentiles are not additive: a host with 0.2 % of the traffic counts as much as one with
    40 %, and the result is a latency that no population of requests has.
    """
    values = [h.percentile(q) for h in histograms]
    if not values:
        raise ValidationError("no histograms to combine")
    return statistics.fmean(values)


def merged_percentile(histograms: Iterable[Histogram], q: float) -> float:
    """The right answer: add the bucket counts, then read the percentile of the whole fleet."""
    hists = list(histograms)
    if not hists:
        raise ValidationError("no histograms to combine")
    return functools.reduce(Histogram.merge, hists).percentile(q)


def bimodal_latencies(
    rng: random.Random, n: int, fast_ms: float, slow_ms: float, slow_ratio: float
) -> list[float]:
    """``n`` synthetic latencies: a log-normal mode around ``fast_ms`` (cache hits) and, with
    probability ``slow_ratio``, one around ``slow_ms`` (misses, GC pauses, retries)."""
    if n <= 0 or fast_ms <= 0 or slow_ms <= 0 or not 0 <= slow_ratio <= 1:
        raise ValidationError("n, fast_ms and slow_ms must be positive; slow_ratio in [0, 1]")
    return [
        rng.lognormvariate(math.log(slow_ms if rng.random() < slow_ratio else fast_ms), 0.25)
        for _ in range(n)
    ]


# --8<-- [end:fleet]


def main() -> None:
    rng = random.Random(42)
    samples = bimodal_latencies(rng, 10_000, fast_ms=10, slow_ms=400, slow_ratio=0.05)
    hist = Histogram.exponential(start=1.0, factor=1.25, count=40)
    for value in samples:
        hist.observe(value)
    exact = {q: exact_percentile(samples, q) for q in (50, 90, 99, 99.9)}
    print("one service, 10,000 requests: 95% cache hits near 10 ms, 5% misses near 400 ms")
    print(
        f"  exact:     mean={statistics.fmean(samples):.1f} ms  p50={exact[50]:.1f} ms  "
        f"p90={exact[90]:.1f} ms  p99={exact[99]:.1f} ms  p99.9={exact[99.9]:.1f} ms"
    )
    print(
        f"  histogram: mean={hist.mean:.1f} ms  p50={hist.percentile(50):.1f} ms  "
        f"p90={hist.percentile(90):.1f} ms  p99={hist.percentile(99):.1f} ms  "
        f"p99.9={hist.percentile(99.9):.1f} ms  ({len(hist.bounds)} buckets, edges x1.25)"
    )
    print("  the mean is a latency almost no request has; p50 and p99 describe both modes")

    print()
    print("four pools, 100,000 requests; the canary pool has 0.2% of the traffic and a bad deploy:")
    pools = {
        "us-east": (0.400, 10.0, 300.0, 0.002),
        "us-west": (0.350, 12.0, 300.0, 0.002),
        "eu-west": (0.248, 11.0, 300.0, 0.002),
        "canary": (0.002, 900.0, 900.0, 0.0),
    }
    histograms: list[Histogram] = []
    pooled: list[float] = []
    for name, (share, fast, slow, ratio) in pools.items():
        latencies = bimodal_latencies(rng, round(100_000 * share), fast, slow, ratio)
        pool_hist = Histogram.exponential(start=1.0, factor=1.25, count=40)
        for value in latencies:
            pool_hist.observe(value)
        histograms.append(pool_hist)
        pooled.extend(latencies)
        print(
            f"  {name:<8} share={share:>5.1%}  requests={pool_hist.count:>6,}  "
            f"p99={pool_hist.percentile(99):>8.1f} ms"
        )
    print(
        f"  mean of the four p99s:       {mean_of_percentiles(histograms, 99):>8.1f} ms"
        "  <- wrong: percentiles do not average"
    )
    print(
        f"  p99 of the merged histogram: {merged_percentile(histograms, 99):>8.1f} ms"
        "  <- right: add bucket counts, then read the percentile"
    )
    print(f"  exact p99 of all requests:   {exact_percentile(pooled, 99):>8.1f} ms")
    print("  the fleet p99 hides the canary entirely: slice percentiles by pool and by version")


if __name__ == "__main__":
    main()
