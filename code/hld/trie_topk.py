"""A prefix trie that caches the top-K completions at every node, plus prefix sharding.

The crux of the typeahead design. A query must answer in single-digit milliseconds while the
user is still typing, so the serving path does no ranking at all: every node stores the finished
answer for its own prefix, and ``suggest`` is a walk down the trie followed by a return.

* ``TopKTrie`` holds the cached top-K per node. Reads are O(len(prefix)); a weight change costs
  O(len(term) x k log k) because it repairs the cached list at every node on the term's path.
* ``bump`` is deliberately **increase-only**. A weight that only rises can never force a cached
  list to pull a replacement out of the subtree, which is exactly what would make an update
  O(subtree). Demotions and removals go through ``TopKTrie.build``, which is what the nightly
  pipeline runs on a fresh trie before swapping it in.
* ``ShardedTrie`` splits the keyspace by the first characters of the prefix, so a query with
  enough characters touches one machine. Shorter prefixes have to scatter, which is the reason
  the first keystroke is served from a cache rather than from the trie.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field

from common import ValidationError

DEFAULT_K = 5
SHARD_KEY_LENGTH = 2


def fold(text: str) -> str:
    """Case-fold and collapse whitespace runs; one trailing space stays significant.

    A user who has typed ``car `` wants completions of ``car rental``, not of ``carbon``, so the
    space must survive normalisation even though runs of it must not.
    """
    folded = " ".join(text.split()).lower()
    return folded + " " if folded and text[-1:].isspace() else folded


# --8<-- [start:model]
@dataclass(frozen=True, slots=True)
class Suggestion:
    term: str
    weight: int

    @property
    def rank_key(self) -> tuple[int, str]:
        """Heaviest first, ties broken alphabetically, so the order is total and stable."""
        return (-self.weight, self.term)


@dataclass(slots=True)
class TrieNode:
    children: dict[str, TrieNode] = field(default_factory=dict)
    top: tuple[Suggestion, ...] = ()  # the finished answer for this node's prefix


# --8<-- [end:model]


# --8<-- [start:trie]
class TopKTrie:
    """Every node caches the answer for its prefix, so a query is a walk and a return.

    ``_lock`` guards ``_root``, ``_weights`` and ``_nodes``. In production the serving replica is
    read-only and immutable, and the lock only exists on the builder; it is kept here so the
    incremental ``bump`` path can be exercised under threads.
    """

    def __init__(self, k: int = DEFAULT_K) -> None:
        if k <= 0:
            raise ValidationError("k must be positive")
        self._k = k
        self._root = TrieNode()
        self._weights: dict[str, int] = {}
        self._nodes = 1
        self._lock = threading.RLock()

    @classmethod
    def build(cls, weights: Mapping[str, int], k: int = DEFAULT_K) -> TopKTrie:
        """What the offline rebuild does: a fresh trie from the aggregated counts."""
        trie = cls(k)
        for term, weight in weights.items():
            trie.set_weight(term, weight)
        return trie

    @property
    def k(self) -> int:
        return self._k

    @property
    def node_count(self) -> int:
        with self._lock:
            return self._nodes

    @property
    def term_count(self) -> int:
        with self._lock:
            return len(self._weights)

    def suggest(self, prefix: str) -> list[Suggestion]:
        """O(len(prefix)) pointer hops, then the cached list. No ranking on the read path."""
        node = self._root
        for char in fold(prefix):
            child = node.children.get(char)
            if child is None:
                return []
            node = child
        return list(node.top)

    def set_weight(self, term: str, weight: int) -> None:
        """Set an absolute weight. Used by the builder; the online path uses :meth:`bump`."""
        key = self._term_key(term)
        if weight < 0:
            raise ValidationError("weights cannot be negative")
        with self._lock:
            if weight < self._weights.get(key, 0):
                raise ValidationError("weights only increase; rebuild the trie to lower one")
            self._weights[key] = weight
            self._apply(key, weight)

    def bump(self, term: str, delta: int = 1) -> int:
        """Add ``delta`` to a term's weight and repair every cached list on its path."""
        if delta <= 0:
            raise ValidationError("delta must be positive; demotions need a rebuild")
        key = self._term_key(term)
        with self._lock:
            weight = self._weights.get(key, 0) + delta
            self._weights[key] = weight
            self._apply(key, weight)
            return weight

    def weights(self) -> dict[str, int]:
        """A snapshot the offline pipeline can merge with new counts before rebuilding."""
        with self._lock:
            return dict(self._weights)

    # -- internals ---------------------------------------------------------------------------
    def _apply(self, term: str, weight: int) -> None:
        suggestion = Suggestion(term, weight)
        node = self._root
        self._offer(node, suggestion)
        for char in term:
            child = node.children.get(char)
            if child is None:
                child = node.children[char] = TrieNode()
                self._nodes += 1
            node = child
            self._offer(node, suggestion)

    def _offer(self, node: TrieNode, suggestion: Suggestion) -> None:
        """Re-rank one cached list. Correct for increases because a riser never evicts a peer
        that then has to be replaced from the subtree."""
        top = [entry for entry in node.top if entry.term != suggestion.term]
        top.append(suggestion)
        top.sort(key=lambda entry: entry.rank_key)
        node.top = tuple(top[: self._k])

    @staticmethod
    def _term_key(term: str) -> str:
        key = fold(term)
        if not key.strip():
            raise ValidationError("term must not be empty")
        return key.strip()


# --8<-- [end:trie]


# --8<-- [start:sharding]
class ShardedTrie:
    """One trie per prefix shard, so a query with enough characters touches one machine.

    Hashing the first ``key_length`` characters keeps every completion of a prefix together --
    range-partitioning by letter would put a third of English traffic on the ``s`` shard.
    Prefixes shorter than ``key_length`` are the exception: they scatter to every shard and the
    results are merged, which is why the first keystroke is answered from a cache in practice.
    """

    def __init__(
        self, shards: int = 4, k: int = DEFAULT_K, key_length: int = SHARD_KEY_LENGTH
    ) -> None:
        if shards <= 0 or key_length <= 0:
            raise ValidationError("shards and key_length must be positive")
        self._tries = [TopKTrie(k) for _ in range(shards)]
        self._k = k
        self._key_length = key_length
        self._lock = threading.Lock()
        self._shards_touched = 0

    @property
    def shard_count(self) -> int:
        return len(self._tries)

    @property
    def shards_touched(self) -> int:
        """Total shard lookups served: the fan-out bill this design exists to keep at 1."""
        with self._lock:
            return self._shards_touched

    def shard_of(self, term: str) -> int:
        key = fold(term)[: self._key_length]
        digest = hashlib.blake2b(key.encode(), digest_size=4).digest()
        return int.from_bytes(digest, "big") % len(self._tries)

    def bump(self, term: str, delta: int = 1) -> int:
        return self._tries[self.shard_of(term)].bump(term, delta)

    def suggest(self, prefix: str) -> list[Suggestion]:
        folded = fold(prefix)
        if len(folded) >= self._key_length:
            self._record(1)
            return self._tries[self.shard_of(folded)].suggest(folded)
        merged: list[Suggestion] = []
        for trie in self._tries:
            merged.extend(trie.suggest(folded))
        self._record(len(self._tries))
        merged.sort(key=lambda entry: entry.rank_key)
        return merged[: self._k]

    def _record(self, touched: int) -> None:
        with self._lock:
            self._shards_touched += touched


# --8<-- [end:sharding]


def main() -> None:
    from concurrent.futures import ThreadPoolExecutor

    logs = {
        "car rental": 9_000,
        "car insurance": 7_500,
        "cardiff weather": 4_200,
        "cat videos": 3_100,
        "cats": 2_400,
        "cancel flight": 1_800,
        "carbon footprint": 1_500,
        "carpool lane": 900,
        "caribbean cruise": 700,
        "camera reviews": 5_600,
        "campsite near me": 2_900,
        "canada visa": 6_100,
    }

    trie = TopKTrie.build(logs, k=3)
    print(f"built {trie.term_count} terms into {trie.node_count} nodes, k={trie.k}")
    for prefix in ("", "ca", "car", "car ", "zz"):
        answer = ", ".join(f"{s.term} {s.weight}" for s in trie.suggest(prefix)) or "(no match)"
        print(f"  suggest({prefix!r}) -> {answer}")

    trie.bump("cat videos", 20_000)
    print("a trending burst of +20000 on 'cat videos' repairs 11 cached lists in place:")
    for prefix in ("ca", "cat"):
        answer = ", ".join(f"{s.term} {s.weight}" for s in trie.suggest(prefix))
        print(f"  suggest({prefix!r}) -> {answer}")

    sharded = ShardedTrie(shards=4, k=3)
    for term, weight in logs.items():
        sharded.bump(term, weight)
    before = sharded.shards_touched
    long_answer = [s.term for s in sharded.suggest("car")]
    long_cost = sharded.shards_touched - before
    before = sharded.shards_touched
    short_answer = [s.term for s in sharded.suggest("c")]
    short_cost = sharded.shards_touched - before
    print(f"sharded over {sharded.shard_count} tries by the first 2 characters:")
    print(f"  suggest('car') -> {long_answer}  ({long_cost} shard touched)")
    print(f"  suggest('c')   -> {short_answer}  ({short_cost} shards touched, so cache it)")

    hot = TopKTrie(k=3)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: hot.bump(f"query {i % 5}"), range(4_000)))
    live = ", ".join(f"{s.term}={s.weight}" for s in hot.suggest("query"))
    print(f"8 threads x 500 bumps: {live}")


if __name__ == "__main__":
    main()
