from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError
from hld.consistent_hashing import (
    RING_SIZE,
    HashRing,
    assignments,
    keys_moved,
    load_stats,
    mod_assignments,
    ring_hash,
)

KEYS = [f"key:{i}" for i in range(5_000)]
NODES = ["A", "B", "C", "D"]


def test_ring_is_a_pure_function_of_membership() -> None:
    forward = HashRing(NODES, vnodes=50)
    backward = HashRing(reversed(NODES), vnodes=50)
    assert assignments(forward, KEYS) == assignments(backward, KEYS)
    assert len(forward) == len(NODES) * 50
    assert ring_hash("user:42") == ring_hash("user:42")
    assert all(0 <= ring_hash(key) < RING_SIZE for key in KEYS[:100])


def test_adding_a_node_moves_about_one_nth_and_only_onto_the_new_node() -> None:
    ring = HashRing(NODES, vnodes=100)
    before = assignments(ring, KEYS)
    ring.add_node("E")
    after = assignments(ring, KEYS)
    stats = keys_moved(before, after)
    assert 0.12 <= stats.fraction <= 0.28  # expected 1/5 of the keys
    assert all(after[key] == "E" for key, node in before.items() if after[key] != node)


def test_removing_a_node_moves_exactly_its_own_keys() -> None:
    ring = HashRing(NODES, vnodes=100)
    before = assignments(ring, KEYS)
    ring.remove_node("B")
    after = assignments(ring, KEYS)
    moved = {key for key, node in before.items() if after[key] != node}
    assert moved == {key for key, node in before.items() if node == "B"}
    assert "B" not in after.values()
    assert ring.nodes == ["A", "C", "D"]


def test_mod_n_hashing_remaps_most_keys() -> None:
    before = mod_assignments(NODES, KEYS)
    after = mod_assignments([*NODES, "E"], KEYS)
    assert keys_moved(before, after).fraction > 0.7  # expected 4/5 of the keys


def test_virtual_nodes_even_out_the_load() -> None:
    names = [f"n{i}" for i in range(8)]
    single = load_stats(assignments(HashRing(names, vnodes=1), KEYS), names)
    many = load_stats(assignments(HashRing(names, vnodes=200), KEYS), names)
    assert sum(single.per_node.values()) == sum(many.per_node.values()) == len(KEYS)
    assert many.peak_to_mean < single.peak_to_mean
    assert many.peak_to_mean < 1.25


def test_weight_scales_a_nodes_share_of_keys() -> None:
    ring = HashRing(["A", "B", "C"], vnodes=100)
    ring.add_node("big", weight=3)  # 3 of 6 weight units -> about half the keys
    share = load_stats(assignments(ring, KEYS), ring.nodes).per_node["big"] / len(KEYS)
    assert 0.42 <= share <= 0.58


def test_preference_list_is_distinct_and_slides_clockwise_on_failure() -> None:
    ring = HashRing(NODES, vnodes=100)
    prefs = ring.preference_list("order:7", replicas=3)
    assert len(prefs) == len(set(prefs)) == 3
    assert prefs[0] == ring.get_node("order:7")
    ring.remove_node(prefs[0])
    assert ring.preference_list("order:7", replicas=2) == prefs[1:]


@pytest.mark.parametrize("replicas", [0, -1])
def test_preference_list_rejects_non_positive_replicas(replicas: int) -> None:
    with pytest.raises(ValidationError):
        HashRing(NODES).preference_list("k", replicas=replicas)


def test_validation_and_state_errors() -> None:
    with pytest.raises(ValidationError):
        HashRing(vnodes=0)
    ring = HashRing()
    with pytest.raises(InvalidStateError):
        ring.get_node("k")
    with pytest.raises(InvalidStateError):
        ring.preference_list("k")
    with pytest.raises(ValidationError):
        ring.add_node("")
    with pytest.raises(ValidationError):
        ring.add_node("A", weight=0)
    ring.add_node("A")
    with pytest.raises(ConflictError):
        ring.add_node("A")
    with pytest.raises(NotFoundError):
        ring.remove_node("Z")
    with pytest.raises(ValidationError):
        ring.preference_list("k", replicas=2)  # only one physical node on the ring


def test_concurrent_membership_changes_and_lookups() -> None:
    ring = HashRing(NODES, vnodes=20)
    valid = set(NODES) | {f"tmp{i}" for i in range(100)}

    def churn(i: int) -> list[str]:
        ring.add_node(f"tmp{i}")
        owners = [ring.get_node(key) for key in KEYS[:100]]
        ring.remove_node(f"tmp{i}")
        return owners

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(churn, range(100)))
    assert all(owner in valid for owners in results for owner in owners)
    assert ring.nodes == NODES
    assert len(ring) == len(NODES) * 20
    assert assignments(ring, KEYS) == assignments(HashRing(NODES, vnodes=20), KEYS)
