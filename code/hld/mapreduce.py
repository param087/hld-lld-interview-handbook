"""MapReduce in miniature: splits, map, combine, partition, shuffle, reduce — and word count.

What the module demonstrates, in the order an interviewer asks about it:

* ``make_splits`` cuts the input into the units of parallelism. One map task per split is the
  whole scaling story: 1,000 splits over 100 workers is 10 waves, and a straggler costs one
  split, not one worker's share of the file.
* ``MapReduceJob.run`` executes map, shuffle and reduce. The same job runs sequentially or
  across processes (``pool_size``) and must produce identical output, which is exactly what
  determinism buys you: failed tasks are simply re-run.
* The **combiner** is a reducer applied inside the map task before anything crosses the
  network. For word count it collapses ``(the, 1) x 400`` into ``(the, 400)``; the demo prints
  the shuffle volume with and without it.
* ``partition_of`` routes a key to a reducer with a *stable* hash, because the map tasks run
  in different processes and every one of them must agree on where a key belongs.

There is no shared mutable state: map tasks return their output, and only the driver merges.
That is why the same code runs in one process or many.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from common import ValidationError

type Pair = tuple[str, int]
type Mapper = Callable[[Split], list[Pair]]
type Reducer = Callable[[str, list[int]], int]


# --8<-- [start:splits]
@dataclass(frozen=True, slots=True)
class Split:
    """One input split: the unit of work handed to a single map task."""

    split_id: int
    records: tuple[str, ...]


def make_splits(records: Sequence[str], splits: int) -> list[Split]:
    """Cut ``records`` into ``splits`` roughly equal chunks, in order.

    Real MapReduce splits by block (64-128 MB) so a map task reads one machine's local disk.
    Make splits smaller than you think: more splits means finer load balancing and a cheaper
    re-run when a task fails, at the cost of one scheduling decision each.
    """
    if splits < 1:
        raise ValidationError("splits must be >= 1")
    size, extra = divmod(len(records), splits)
    out: list[Split] = []
    start = 0
    for index in range(splits):
        end = start + size + (1 if index < extra else 0)
        out.append(Split(split_id=index, records=tuple(records[start:end])))
        start = end
    return out


WORD = re.compile(r"[a-z0-9]+")


def word_count_map(split: Split) -> list[Pair]:
    """The classic map function: one ``(word, 1)`` pair per token. Runs in a worker process."""
    return [(word, 1) for record in split.records for word in WORD.findall(record.lower())]


def sum_values(key: str, values: list[int]) -> int:
    """The reducer (and the combiner: summing is associative and commutative, so both work)."""
    del key
    return sum(values)


def partition_of(key: str, reducers: int) -> int:
    """Reducer for ``key``, from a stable hash: every map process must route a key identically.

    Python's built-in ``hash()`` is salted per process, so with ``spawn`` workers it would send
    the same word to different reducers and silently split the count in two.
    """
    if reducers < 1:
        raise ValidationError("reducers must be >= 1")
    digest = hashlib.md5(key.encode(), usedforsecurity=False).digest()
    return int.from_bytes(digest[:4], "big") % reducers


# --8<-- [end:splits]


# --8<-- [start:job]
@dataclass(frozen=True, slots=True)
class MapOutput:
    """What one map task hands back: emitted pair count, plus the routed pairs to shuffle."""

    emitted: int
    routed: tuple[tuple[int, str, int], ...]  # (reducer_id, key, value)


@dataclass(frozen=True, slots=True)
class ShuffleStats:
    """The number that decides whether a MapReduce job finishes: bytes across the network."""

    map_pairs: int  # pairs emitted by the map functions
    shuffled_pairs: int  # pairs that actually crossed to the reducers
    reduce_keys: int

    @property
    def reduction(self) -> float:
        """Share of pairs the combiner removed before the shuffle."""
        return 1 - self.shuffled_pairs / self.map_pairs if self.map_pairs else 0.0


@dataclass(frozen=True, slots=True)
class JobResult:
    by_reducer: dict[int, dict[str, int]]
    stats: ShuffleStats

    @property
    def totals(self) -> dict[str, int]:
        """Every reducer's output merged; keys never collide because partitioning is by key."""
        return {key: value for part in self.by_reducer.values() for key, value in part.items()}

    def top(self, n: int) -> list[Pair]:
        """The ``n`` highest counts, ties broken alphabetically so the output is deterministic."""
        return sorted(self.totals.items(), key=lambda item: (-item[1], item[0]))[:n]


class MapReduceJob:
    """Map, shuffle and reduce over a list of splits, in this process or in a process pool."""

    def __init__(
        self,
        mapper: Mapper,
        reducer: Reducer,
        reducers: int = 4,
        combiner: Reducer | None = None,
    ) -> None:
        if reducers < 1:
            raise ValidationError("reducers must be >= 1")
        self._mapper = mapper
        self._reducer = reducer
        self._reducers = reducers
        self._combiner = combiner

    @property
    def reducers(self) -> int:
        return self._reducers

    def map_task(self, split: Split) -> MapOutput:
        """Map one split, combine locally, then route each key to its reducer."""
        pairs = self._mapper(split)
        emitted = len(pairs)
        if self._combiner is not None:
            grouped: dict[str, list[int]] = defaultdict(list)
            for key, value in pairs:
                grouped[key].append(value)
            pairs = [(key, self._combiner(key, values)) for key, values in grouped.items()]
        routed = tuple((partition_of(key, self._reducers), key, value) for key, value in pairs)
        return MapOutput(emitted=emitted, routed=routed)

    def run(self, splits: Sequence[Split], pool_size: int | None = None) -> JobResult:
        """Run the job; ``pool_size`` runs the map phase in that many worker processes.

        The reduce phase stays in the driver because this is a teaching model: in a real
        cluster each reducer pulls its partition from every mapper's local disk, and that
        pull *is* the shuffle.
        """
        if not splits:
            raise ValidationError("a job needs at least one split")
        if pool_size is None:
            outputs = [self.map_task(split) for split in splits]
        else:
            if pool_size < 1:
                raise ValidationError("pool_size must be >= 1")
            with multiprocessing.Pool(pool_size) as pool:
                outputs = pool.map(self.map_task, splits)

        buckets: dict[int, dict[str, list[int]]] = {r: defaultdict(list) for r in range(self._reducers)}
        map_pairs = shuffled = 0
        for output in outputs:
            map_pairs += output.emitted
            for reducer_id, key, value in output.routed:
                buckets[reducer_id][key].append(value)
                shuffled += 1

        by_reducer = {
            reducer_id: {key: self._reducer(key, values) for key, values in sorted(bucket.items())}
            for reducer_id, bucket in buckets.items()
        }
        keys = sum(len(part) for part in by_reducer.values())
        return JobResult(by_reducer=by_reducer, stats=ShuffleStats(map_pairs, shuffled, keys))


# --8<-- [end:job]


CORPUS: tuple[str, ...] = (
    "MapReduce splits the input, runs a map task per split and sorts the output by key.",
    "The shuffle moves every key to the reducer that owns it, and the shuffle is the bottleneck.",
    "A combiner runs the reducer inside the map task, so the shuffle carries far fewer pairs.",
    "Spark keeps the working set in memory and describes the job as a DAG of stages.",
    "A stage boundary is a shuffle, so a job with fewer shuffles is a job that finishes.",
    "Flink treats the batch job as a bounded stream, so one engine serves both shapes.",
    "Event time is when the event happened; processing time is when the machine saw it.",
    "A watermark says the engine believes no event older than this time will arrive.",
    "A tumbling window is a sliding window whose slide equals its size.",
    "Late events arrive after the watermark, so a policy decides whether to update or drop.",
)


def main() -> None:
    records = [line for _ in range(40) for line in CORPUS]
    splits = make_splits(records, 8)
    print(f"input: {len(records)} lines cut into {len(splits)} splits of ~{len(splits[0].records)} lines")

    plain = MapReduceJob(word_count_map, sum_values, reducers=3)
    combined = MapReduceJob(word_count_map, sum_values, reducers=3, combiner=sum_values)

    no_combiner = plain.run(splits)
    with_combiner = combined.run(splits)
    print(
        f"map emitted {no_combiner.stats.map_pairs:,} pairs; shuffled "
        f"{no_combiner.stats.shuffled_pairs:,} without a combiner, "
        f"{with_combiner.stats.shuffled_pairs:,} with one "
        f"({with_combiner.stats.reduction:.0%} less network)"
    )

    parallel = combined.run(splits, pool_size=4)
    print(f"4 worker processes give the same answer: {parallel.totals == with_combiner.totals}")

    sizes = " ".join(
        f"r{reducer_id}={len(part)} keys" for reducer_id, part in sorted(parallel.by_reducer.items())
    )
    print(f"{parallel.stats.reduce_keys} distinct words over 3 reducers: {sizes}")
    top = " ".join(f"{word}={count}" for word, count in parallel.top(8))
    print(f"top 8: {top}")
    print(f"'shuffle' -> reducer {partition_of('shuffle', 3)}, 'window' -> reducer {partition_of('window', 3)}")

    skewed = make_splits(["the " * 200, *records], 8)
    skew = combined.run(skewed)
    counts = [sum(part.values()) for part in skew.by_reducer.values()]
    print(
        f"one hot key added: reducer loads {counts}, "
        f"peak/mean={max(counts) / (sum(counts) / len(counts)):.2f} - this is reducer skew"
    )


if __name__ == "__main__":
    main()
