import math
import random

import pytest

from common import ValidationError
from hld.merkle_tree import MerkleTree

ITEMS = {f"user:{i}": f"profile-{i}" for i in range(2_000)}


def test_root_ignores_insertion_order_and_reflects_content() -> None:
    forward = MerkleTree(ITEMS, leaves=64)
    backward = MerkleTree(dict(reversed(list(ITEMS.items()))), leaves=64)
    assert forward.root == backward.root
    changed = MerkleTree({**ITEMS, "user:7": "profile-7-edited"}, leaves=64)
    assert changed.root != forward.root
    assert MerkleTree({}, leaves=64).root != forward.root
    assert MerkleTree(ITEMS, leaves=32).root != forward.root  # a different shape is a different tree


def test_identical_trees_differ_nowhere_and_cost_one_comparison() -> None:
    a, b = MerkleTree(ITEMS, leaves=256), MerkleTree(dict(ITEMS), leaves=256)
    result = a.diff(b)
    assert result.buckets == () and result.comparisons == 1


@pytest.mark.parametrize("leaves", [1, 2, 16, 1_024])
def test_single_change_is_found_in_one_bucket_with_logarithmic_cost(leaves: int) -> None:
    a = MerkleTree(ITEMS, leaves=leaves)
    b = MerkleTree({**ITEMS, "user:1234": "stale"}, leaves=leaves)
    result = a.diff(b)
    assert result.buckets == (a.bucket_of("user:1234"),)
    assert "user:1234" in a.keys_in_bucket(result.buckets[0])
    assert result.comparisons <= 2 * int(math.log2(leaves)) + 1


def test_every_difference_lies_in_a_reported_bucket_and_every_reported_bucket_differs() -> None:
    rng = random.Random(42)
    replica = dict(ITEMS)
    changed = {f"user:{rng.randrange(2_000)}" for _ in range(10)}
    missing = {f"user:{rng.randrange(2_000)}" for _ in range(5)} - changed
    extra = {f"ghost:{i}" for i in range(3)}
    for key in changed:
        replica[key] = "stale"
    for key in missing:
        del replica[key]
    for key in extra:
        replica[key] = "ghost"
    a, b = MerkleTree(ITEMS, leaves=512), MerkleTree(replica, leaves=512)
    result = a.diff(b)
    expected = {a.bucket_of(key) for key in changed | missing | extra}
    assert set(result.buckets) == expected
    assert result.comparisons < len(ITEMS)
    assert b.diff(a).buckets == result.buckets  # symmetric


@pytest.mark.parametrize("leaves", [1, 4, 128])
def test_repairing_the_differing_buckets_makes_the_roots_equal(leaves: int) -> None:
    replica = dict(ITEMS)
    replica["user:10"] = "old"
    del replica["user:20"]
    replica["user:9999"] = "should not exist"
    primary, stale = MerkleTree(ITEMS, leaves=leaves), MerkleTree(replica, leaves=leaves)
    for bucket in primary.diff(stale).buckets:
        for key in stale.keys_in_bucket(bucket):
            if key not in ITEMS:
                del replica[key]
        for key in primary.keys_in_bucket(bucket):
            replica[key] = ITEMS[key]
    assert MerkleTree(replica, leaves=leaves).root == primary.root
    assert replica == ITEMS


def test_validation_errors() -> None:
    with pytest.raises(ValidationError):
        MerkleTree(ITEMS, leaves=0)
    with pytest.raises(ValidationError):
        MerkleTree(ITEMS, leaves=12)
    a, b = MerkleTree(ITEMS, leaves=8), MerkleTree(ITEMS, leaves=16)
    with pytest.raises(ValidationError):
        a.diff(b)
    with pytest.raises(ValidationError):
        a.keys_in_bucket(8)
