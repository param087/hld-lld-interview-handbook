import random
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, InvalidStateError, NotFoundError, ValidationError
from hld.cache_cluster import CacheCluster, CacheNode, CacheOutcome

NODES = ["c1", "c2", "c3", "c4"]


def cluster(**kwargs: object) -> CacheCluster:
    defaults: dict[str, object] = {"capacity_per_node": 100, "replicas": 2, "clock": FakeClock()}
    return CacheCluster(NODES, **{**defaults, **kwargs})  # type: ignore[arg-type]


def test_routing_is_stable_and_adding_a_node_moves_about_one_fifth_of_the_keys() -> None:
    ring = cluster()
    keys = [f"user:{i}" for i in range(5_000)]
    placement = {key: ring.node_for(key) for key in keys}
    assert {ring.node_for(key) for key in keys} == set(NODES), "every node gets keys"
    assert all(ring.node_for(key) == node for key, node in placement.items()), "routing is a function"
    moved = ring.add_node("c5", keys)
    assert 0.10 < moved < 0.35, f"consistent hashing moved {moved:.0%}, expected ~1/5"
    changed = [key for key, node in placement.items() if ring.node_for(key) != node]
    assert all(ring.node_for(key) == "c5" for key in changed), "keys only move onto the new node"


def test_a_miss_hands_out_one_lease_and_everyone_else_is_told_to_wait() -> None:
    node = CacheNode("c1", capacity=10, lease_ttl=5.0, clock=FakeClock(start=100.0))
    first = node.get("user:1")
    assert first.outcome is CacheOutcome.LEASE and first.lease is not None and first.value is None
    second = node.get("user:1")
    assert second.outcome is CacheOutcome.WAIT and second.lease is None

    assert node.set("user:1", "ann", first.lease) is True
    hit = node.get("user:1")
    assert (hit.outcome, hit.value) == (CacheOutcome.HIT, "ann")
    # the lease is consumed: a replayed fill with the same token is refused
    assert node.set("user:1", "replay", first.lease) is False
    assert node.rejected_sets == 1


def test_invalidation_revokes_the_lease_so_a_slow_client_cannot_write_back_a_stale_value() -> None:
    node = CacheNode("c1", capacity=10, clock=FakeClock(start=100.0))
    slow = node.get("stock:1")  # client A misses and takes the lease, then goes to the database
    assert slow.lease is not None
    node.invalidate("stock:1")  # meanwhile the row changes and the writer invalidates the key
    assert node.set("stock:1", "old-price", slow.lease) is False
    assert node.rejected_sets == 1
    assert node.get("stock:1").outcome is CacheOutcome.LEASE, "the key is empty, not poisoned"


def test_stale_copies_absorb_the_herd_while_one_client_refills() -> None:
    node = CacheNode("c1", capacity=10, clock=FakeClock(start=100.0))
    filler = node.get("hot:1").lease
    assert filler is not None and node.set("hot:1", "v1", filler, 60) is True
    node.invalidate("hot:1", keep_stale=True)
    refiller = node.get("hot:1")
    assert (refiller.outcome, refiller.value) == (CacheOutcome.LEASE, "v1")
    waiter = node.get("hot:1")
    assert (waiter.outcome, waiter.value, waiter.lease) == (CacheOutcome.WAIT, "v1", None)
    assert refiller.lease is not None and node.set("hot:1", "v2", refiller.lease, 60) is True
    refreshed = node.get("hot:1")
    assert (refreshed.outcome, refreshed.value, refreshed.lease) == (CacheOutcome.HIT, "v2", None)


def test_lazy_expiry_holds_memory_until_the_active_sweep_reclaims_it() -> None:
    clock = FakeClock(start=1_000.0)
    node = CacheNode("c1", capacity=200, clock=clock)
    for i in range(100):
        node.put(f"k:{i}", "v", ttl=30)
    assert len(node) == 100 and len(node.live_keys()) == 100
    clock.advance(31)
    assert len(node) == 100, "lazy expiry frees nothing until something touches the key"
    assert node.live_keys() == []
    reclaimed = node.active_expire(sample=40, rng=random.Random(42))
    assert reclaimed == 40 and len(node) == 60
    assert node.active_expire(sample=1_000, rng=random.Random(42)) == 60 and len(node) == 0


def test_a_node_failure_reroutes_its_keys_to_a_cold_replica() -> None:
    ring = cluster()
    ring.put("user:42", "ann", ttl=60)
    owner = ring.node_for("user:42")
    assert ring.get("user:42").outcome is CacheOutcome.HIT
    ring.fail(owner)
    standby = ring.node_for("user:42")
    assert standby != owner
    assert ring.get("user:42").outcome is CacheOutcome.LEASE, "the replica is cold, not a copy"
    ring.recover(owner)
    assert ring.node_for("user:42") == owner and ring.get("user:42").value == "ann"
    for node in NODES:
        ring.fail(node)
    with pytest.raises(InvalidStateError):
        ring.node_for("user:42")


def test_concurrent_readers_of_one_cold_key_cause_a_single_database_load() -> None:
    ring = cluster(wait=0.0005)
    calls: list[str] = []
    guard = threading.Lock()

    def database(key: str) -> str:
        with guard:
            calls.append(key)
        return f"row:{key}"

    readers = 32
    with ThreadPoolExecutor(max_workers=readers) as pool:
        values = list(pool.map(lambda _: ring.load_through("hot:1", database, ttl=60), range(readers)))

    assert values == ["row:hot:1"] * readers
    assert calls == ["hot:1"], "the lease coalesced 32 misses into one load"
    assert ring.get("hot:1").outcome is CacheOutcome.HIT


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"nodes": [], "replicas": 1}, ValidationError),
        ({"nodes": NODES, "replicas": 0}, ValidationError),
        ({"nodes": NODES, "replicas": 9}, ValidationError),
    ],
)
def test_cluster_configuration_is_validated(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        CacheCluster(**kwargs)  # type: ignore[arg-type]


def test_node_configuration_and_unknown_nodes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CacheNode("c1", capacity=10, lease_ttl=0)
    node = CacheNode("c1", capacity=10, clock=FakeClock())
    with pytest.raises(ValidationError):
        node.active_expire(sample=0, rng=random.Random(1))
    with pytest.raises(NotFoundError):
        cluster().node("nobody")
