"""Tests for the MapReduce word-count engine."""

from __future__ import annotations

import pytest

from common import ValidationError
from hld.mapreduce import (
    MapReduceJob,
    Split,
    make_splits,
    partition_of,
    sum_values,
    word_count_map,
)

LINES = (
    "the shuffle moves every key to the reducer that owns it",
    "a combiner runs the reducer inside the map task",
    "the shuffle is the bottleneck of every batch job",
    "event time is not processing time",
)


def counts(records: tuple[str, ...] = LINES) -> dict[str, int]:
    """The answer computed the obvious way, to check MapReduce against."""
    out: dict[str, int] = {}
    for record in records:
        for word in record.split():
            out[word] = out.get(word, 0) + 1
    return out


@pytest.mark.parametrize(("records", "splits", "sizes"), [
    (10, 1, [10]),
    (10, 2, [5, 5]),
    (10, 3, [4, 3, 3]),
    (10, 4, [3, 3, 2, 2]),
    (3, 5, [1, 1, 1, 0, 0]),
])
def test_splits_cover_every_record_and_differ_by_at_most_one(
    records: int, splits: int, sizes: list[int]
) -> None:
    data = [f"line {i}" for i in range(records)]
    parts = make_splits(data, splits)
    assert [len(part.records) for part in parts] == sizes
    assert [part.split_id for part in parts] == list(range(splits))
    assert [record for part in parts for record in part.records] == data


def test_partitioning_is_stable_and_spreads_keys() -> None:
    # a stable digest, not the salted built-in hash: every map process must agree
    assert partition_of("shuffle", 3) == partition_of("shuffle", 3) == 2
    assert partition_of("window", 3) == 2
    spread = {partition_of(f"key{i}", 4) for i in range(200)}
    assert spread == {0, 1, 2, 3}
    with pytest.raises(ValidationError):
        partition_of("k", 0)


def test_word_count_lowercases_and_drops_punctuation() -> None:
    split = Split(split_id=0, records=("The shuffle, the SHUFFLE!", "a map-task"))
    assert word_count_map(split) == [
        ("the", 1),
        ("shuffle", 1),
        ("the", 1),
        ("shuffle", 1),
        ("a", 1),
        ("map", 1),
        ("task", 1),
    ]


def test_the_combiner_cuts_the_shuffle_without_changing_the_answer() -> None:
    splits = make_splits(list(LINES), 2)
    plain = MapReduceJob(word_count_map, sum_values, reducers=3)
    combined = MapReduceJob(word_count_map, sum_values, reducers=3, combiner=sum_values)

    without = plain.run(splits)
    with_combiner = combined.run(splits)

    assert without.totals == with_combiner.totals == counts()
    assert without.stats.map_pairs == with_combiner.stats.map_pairs
    assert without.stats.shuffled_pairs == without.stats.map_pairs  # nothing was collapsed
    assert with_combiner.stats.shuffled_pairs < without.stats.shuffled_pairs
    assert 0 < with_combiner.stats.reduction < 1
    assert without.stats.reduction == 0.0


def test_reducers_hold_disjoint_key_ranges_and_top_breaks_ties_by_word() -> None:
    splits = make_splits(list(LINES), 2)
    result = MapReduceJob(word_count_map, sum_values, reducers=4, combiner=sum_values).run(splits)

    parts = [set(part) for part in result.by_reducer.values()]
    assert len(result.by_reducer) == 4
    assert set().union(*parts) == set(counts())
    assert sum(len(part) for part in parts) == len(set(counts()))  # no key in two reducers
    for reducer_id, part in result.by_reducer.items():
        assert all(partition_of(key, 4) == reducer_id for key in part)

    assert result.stats.reduce_keys == len(counts())
    assert result.top(4) == [("the", 6), ("every", 2), ("is", 2), ("reducer", 2)]  # ties alphabetical


def test_the_process_pool_produces_the_same_answer_as_one_process() -> None:
    splits = make_splits(list(LINES) * 4, 4)
    job = MapReduceJob(word_count_map, sum_values, reducers=3, combiner=sum_values)

    sequential = job.run(splits)
    parallel = job.run(splits, pool_size=2)

    assert parallel.totals == sequential.totals == {word: n * 4 for word, n in counts().items()}
    assert parallel.by_reducer == sequential.by_reducer
    assert parallel.stats == sequential.stats


def test_invalid_arguments_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_splits(["a"], 0)
    with pytest.raises(ValidationError):
        MapReduceJob(word_count_map, sum_values, reducers=0)
    job = MapReduceJob(word_count_map, sum_values, reducers=2)
    with pytest.raises(ValidationError):
        job.run([])
    with pytest.raises(ValidationError):
        job.run(make_splits(["a b"], 1), pool_size=0)
