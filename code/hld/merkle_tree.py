"""Merkle trees for anti-entropy: find the keys two replicas disagree on by exchanging hashes.

What the module demonstrates, in the order an interviewer asks about it:

* ``MerkleTree`` buckets a replica's keys into ``leaves`` ranges by hash, hashes each bucket's
  sorted key-value pairs, and folds the hashes pairwise up to a single root.
* Two replicas with equal roots hold identical data; ``diff`` walks both trees top-down and
  descends only where hashes differ, so a few differing keys cost about 2 * log2(leaves)
  comparisons each instead of a full key-by-key exchange.
* ``keys_in_bucket`` names the keys to ship once a differing leaf is found, which is how
  Cassandra's repair and Dynamo's anti-entropy bound their network cost.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from common import ValidationError


def _digest(*parts: bytes) -> bytes:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part)
    return hasher.digest()


# --8<-- [start:tree]
@dataclass(frozen=True, slots=True)
class DiffResult:
    """Leaf buckets whose contents differ, and how many hash pairs it took to find them."""

    buckets: tuple[int, ...]
    comparisons: int


class MerkleTree:
    """A hash tree over a replica's key-value pairs, ``leaves`` buckets wide (a power of two).

    ``_levels[0]`` holds one hash per bucket; each level above hashes adjacent pairs, so
    ``_levels[-1]`` is the single root. Two trees are comparable only when built with the same
    ``leaves``, because the bucket of a key is ``hash(key) mod leaves``.
    """

    def __init__(self, items: Mapping[str, str], leaves: int = 16) -> None:
        if leaves < 1 or leaves & (leaves - 1):
            raise ValidationError("leaves must be a power of two")
        self._leaves = leaves
        self._buckets: list[dict[str, str]] = [{} for _ in range(leaves)]
        for key, value in items.items():
            self._buckets[self.bucket_of(key)][key] = value
        level = [self._leaf_hash(bucket) for bucket in self._buckets]
        self._levels = [level]
        while len(level) > 1:
            level = [_digest(level[i], level[i + 1]) for i in range(0, len(level), 2)]
            self._levels.append(level)

    @staticmethod
    def _leaf_hash(bucket: Mapping[str, str]) -> bytes:
        pairs = (f"{key}={bucket[key]}\n".encode() for key in sorted(bucket))
        return _digest(*pairs)

    @property
    def leaves(self) -> int:
        return self._leaves

    @property
    def root(self) -> str:
        return self._levels[-1][0].hex()

    def bucket_of(self, key: str) -> int:
        digest = hashlib.sha256(key.encode()).digest()
        return int.from_bytes(digest[:8], "big") % self._leaves

    def keys_in_bucket(self, bucket: int) -> list[str]:
        if not 0 <= bucket < self._leaves:
            raise ValidationError(f"bucket {bucket} out of range")
        return sorted(self._buckets[bucket])

    def diff(self, other: MerkleTree) -> DiffResult:
        """Top-down walk of both trees; descend only into subtrees whose hashes differ."""
        if other.leaves != self._leaves:
            raise ValidationError("trees must have the same number of leaves to be compared")
        differing: list[int] = []
        comparisons = 0
        pending = [(len(self._levels) - 1, 0)]  # (level, index), starting at the root
        while pending:
            level, index = pending.pop()
            comparisons += 1
            if self._levels[level][index] == other._levels[level][index]:
                continue
            if level == 0:
                differing.append(index)
            else:
                pending.extend([(level - 1, 2 * index + 1), (level - 1, 2 * index)])
        return DiffResult(tuple(sorted(differing)), comparisons)


# --8<-- [end:tree]


def main() -> None:
    primary = {f"user:{i}": f"profile-{i}" for i in range(10_000)}
    replica = dict(primary)
    replica["user:123"] = "profile-123-stale"  # a missed update
    del replica["user:4567"]  # a missed insert
    replica["user:99999"] = "ghost"  # a missed delete
    leaves = 1_024
    tree_a, tree_b = MerkleTree(primary, leaves), MerkleTree(replica, leaves)
    print(f"{len(primary):,} keys per replica, Merkle tree with {leaves:,} leaves ({len(tree_a._levels)} levels)")
    print(f"roots equal: {tree_a.root == tree_b.root}  (A {tree_a.root[:12]}..., B {tree_b.root[:12]}...)")
    result = tree_a.diff(tree_b)
    print(
        f"diff: {len(result.buckets)} buckets differ, found with {result.comparisons} hash comparisons "
        f"instead of exchanging {len(primary):,} keys"
    )
    for bucket in result.buckets:
        keys = sorted(set(tree_a.keys_in_bucket(bucket)) | set(tree_b.keys_in_bucket(bucket)))
        suspects = [key for key in keys if primary.get(key) != replica.get(key)]
        print(f"  bucket {bucket:>4}: {len(keys)} keys to compare, mismatched {suspects}")
        for key in tree_a.keys_in_bucket(bucket):
            replica[key] = primary[key]
        for key in tree_b.keys_in_bucket(bucket):
            if key not in primary:
                del replica[key]
    repaired = MerkleTree(replica, leaves)
    print(f"after repairing those buckets: roots equal: {tree_a.root == repaired.root}, diff comparisons: {tree_a.diff(repaired).comparisons}")


if __name__ == "__main__":
    main()
