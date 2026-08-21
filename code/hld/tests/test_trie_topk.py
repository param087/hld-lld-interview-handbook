from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.trie_topk import ShardedTrie, Suggestion, TopKTrie, fold

LOGS = {
    "car rental": 9_000,
    "car insurance": 7_500,
    "cardiff weather": 4_200,
    "cat videos": 3_100,
    "cats": 2_400,
    "cancel flight": 1_800,
    "carbon footprint": 1_500,
    "camera reviews": 5_600,
    "canada visa": 6_100,
}


@pytest.fixture
def trie() -> TopKTrie:
    return TopKTrie.build(LOGS, k=3)


def brute_force(prefix: str, k: int) -> list[Suggestion]:
    """The answer a full scan would give; the cached lists must match it exactly."""
    matches = [Suggestion(term, weight) for term, weight in LOGS.items() if term.startswith(prefix)]
    matches.sort(key=lambda entry: entry.rank_key)
    return matches[:k]


def test_suggest_returns_the_cached_top_k_in_rank_order(trie: TopKTrie) -> None:
    assert [s.term for s in trie.suggest("car")] == [
        "car rental",
        "car insurance",
        "cardiff weather",
    ]
    assert [s.weight for s in trie.suggest("car")] == [9_000, 7_500, 4_200]
    assert len(trie.suggest("ca")) == 3  # never longer than k
    assert trie.suggest("") == trie.suggest("ca")  # every term here starts with 'ca'
    assert trie.suggest("zz") == []
    assert trie.term_count == len(LOGS)
    assert trie.node_count > len(LOGS)


def test_every_node_matches_a_full_scan(trie: TopKTrie) -> None:
    prefixes = {term[:i] for term in LOGS for i in range(len(term) + 1)}
    for prefix in sorted(prefixes):
        assert trie.suggest(prefix) == brute_force(prefix, 3), prefix


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  Car   Rental ", "car rental "), ("CAR", "car"), ("car\trental", "car rental"), ("", "")],
)
def test_folding_collapses_whitespace_but_keeps_a_trailing_space(raw: str, expected: str) -> None:
    assert fold(raw) == expected


def test_a_trailing_space_narrows_the_suggestions(trie: TopKTrie) -> None:
    assert [s.term for s in trie.suggest("car ")] == ["car rental", "car insurance"]
    # without the space, single-word completions such as 'cardiff weather' stay in the answer
    assert "cardiff weather" in [s.term for s in trie.suggest("car")]


def test_bump_promotes_a_term_along_its_whole_path(trie: TopKTrie) -> None:
    assert [s.term for s in trie.suggest("ca")][0] == "car rental"
    assert trie.bump("cat videos", 20_000) == 23_100
    for prefix in ("", "c", "ca", "cat", "cat v"):
        assert trie.suggest(prefix)[0].term == "cat videos", prefix
    assert trie.weights()["cat videos"] == 23_100


def test_bump_creates_terms_that_were_not_in_the_build(trie: TopKTrie) -> None:
    assert trie.suggest("cave") == []
    trie.bump("cave diving", 50_000)
    assert [s.term for s in trie.suggest("cave")] == ["cave diving"]
    assert trie.suggest("ca")[0].term == "cave diving"


def test_invalid_updates_are_rejected(trie: TopKTrie) -> None:
    with pytest.raises(ValidationError):
        trie.bump("cats", 0)
    with pytest.raises(ValidationError):
        trie.bump("   ")
    with pytest.raises(ValidationError):
        trie.set_weight("cats", -1)
    with pytest.raises(ValidationError):
        trie.set_weight("cats", 1)  # demotions need a rebuild, not an in-place edit
    with pytest.raises(ValidationError):
        TopKTrie(k=0)


def test_rebuilding_is_how_a_weight_goes_down(trie: TopKTrie) -> None:
    snapshot = trie.weights()
    snapshot["car rental"] = 10  # a spam filter demotes it overnight
    rebuilt = TopKTrie.build(snapshot, k=3)
    assert [s.term for s in rebuilt.suggest("car")] == [
        "car insurance",
        "cardiff weather",
        "carbon footprint",
    ]
    assert [s.term for s in trie.suggest("car")][0] == "car rental"  # the live trie is untouched


def test_sharding_keeps_a_long_prefix_on_one_shard(trie: TopKTrie) -> None:
    sharded = ShardedTrie(shards=4, k=3, key_length=2)
    for term, weight in LOGS.items():
        sharded.bump(term, weight)

    before = sharded.shards_touched
    assert sharded.suggest("car") == trie.suggest("car")
    assert sharded.shards_touched - before == 1

    before = sharded.shards_touched
    assert sharded.suggest("c") == trie.suggest("c")  # merged answer is identical
    assert sharded.shards_touched - before == 4  # short prefixes scatter

    assert sharded.shard_of("car rental") == sharded.shard_of("carbon footprint")
    with pytest.raises(ValidationError):
        ShardedTrie(shards=0)


def test_concurrent_bumps_are_all_counted() -> None:
    hot = TopKTrie(k=3)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: hot.bump(f"query {i % 5}"), range(4_000)))
    assert [s.weight for s in hot.suggest("query")] == [800, 800, 800]
    assert sum(hot.weights().values()) == 4_000
    assert hot.term_count == 5
