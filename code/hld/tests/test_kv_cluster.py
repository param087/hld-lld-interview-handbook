from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.kv_cluster import KVCluster, VectorClock, Version, reconcile
from hld.quorum import QuorumError

NODES = ["A", "B", "C", "D", "E"]


def clock(**counters: int) -> VectorClock:
    return VectorClock(tuple(sorted(counters.items())))


def test_vector_clock_ordering() -> None:
    base = VectorClock().increment("A")
    later = base.increment("A")
    other = base.increment("B")
    assert later.dominates(base) and not base.dominates(later)
    assert later.concurrent_with(other) and other.concurrent_with(later)
    assert later.merge(other) == clock(A=2, B=1)
    assert later.merge(other).descends_from(later) and later.merge(other).descends_from(other)
    assert str(clock(B=3, A=1)) == "{A:1, B:3}"


def test_reconcile_keeps_only_concurrent_versions() -> None:
    old = Version("v1", clock(A=1))
    left = Version("v2", clock(A=1, B=1))
    right = Version("v3", clock(A=1, C=1))
    newest = Version("v4", clock(A=1, B=1, C=1))
    assert reconcile([old, left, right]) == [left, right]
    assert reconcile([old, left, right, newest, newest]) == [newest]
    assert reconcile([]) == []


def test_put_lands_on_preference_list_and_get_returns_it() -> None:
    cluster = KVCluster(NODES, n=3, w=2, r=2)
    result = cluster.put("user:1", "ann")
    homes = cluster.preference_list("user:1")
    assert len(set(homes)) == 3 and list(result.acked_by) == homes
    assert result.clock == clock(**{homes[0]: 1})  # the first healthy home coordinates
    assert cluster.holders("user:1") == sorted(homes)
    read = cluster.get("user:1")
    assert read.values == ("ann",) and read.context == result.clock and read.repaired == ()
    assert cluster.get("missing").values == ()


def test_concurrent_writes_become_siblings_and_context_reconciles() -> None:
    cluster = KVCluster(NODES)
    cluster.put("cart", "apple")
    ctx = cluster.get("cart").context
    homes = cluster.preference_list("cart")
    cluster.put("cart", "apple,bread", context=ctx, via=homes[1])
    cluster.put("cart", "apple,milk", context=ctx, via=homes[2])
    read = cluster.get("cart")
    assert read.values == ("apple,bread", "apple,milk")
    assert read.context == clock(**{homes[0]: 1, homes[1]: 1, homes[2]: 1})
    cluster.put("cart", "apple,bread,milk", context=read.context)
    assert cluster.get("cart").values == ("apple,bread,milk",)
    # a write that supersedes a read replaces it; a blind write becomes a sibling
    cluster.put("cart", "blind")
    assert cluster.get("cart").values == ("apple,bread,milk", "blind")


def test_sloppy_quorum_hints_then_hands_off_on_recovery() -> None:
    cluster = KVCluster(NODES, n=3, w=2, r=2)
    cluster.put("k", "v1")
    homes = cluster.preference_list("k")
    cluster.fail(homes[0])
    write = cluster.put("k", "v2", context=cluster.get("k").context)
    assert homes[0] not in write.acked_by and len(write.acked_by) == 3
    (stand_in, home), = write.hinted
    assert home == homes[0] and stand_in not in homes
    assert cluster.get("k").values == ("v2",)  # the stand-in serves reads meanwhile
    assert cluster.node(homes[0]).data["k"][0].value == "v1"
    assert cluster.recover(homes[0]) == 1
    assert cluster.node(homes[0]).data["k"][0].value == "v2"
    assert cluster.node(stand_in).hints == {} and "k" not in cluster.node(stand_in).data

    strict = KVCluster(NODES, n=3, w=2, r=2, sloppy=False)
    strict.fail(homes[0])
    strict.fail(homes[1])
    with pytest.raises(QuorumError):
        strict.put("k", "v")
    with pytest.raises(QuorumError):
        strict.get("k")


def test_read_repair_fixes_the_stale_replica_it_touches() -> None:
    cluster = KVCluster(NODES, n=3, w=2, r=3)
    cluster.put("k", "v1")
    homes = cluster.preference_list("k")
    cluster.node(homes[2]).data["k"] = [Version("v0", clock(**{homes[0]: 0}))]  # missed the write
    read = cluster.get("k")
    assert read.values == ("v1",) and read.repaired == (homes[2],)
    assert cluster.node(homes[2]).data["k"][0].value == "v1"
    assert cluster.get("k").repaired == ()


def test_anti_entropy_syncs_only_the_differing_bucket() -> None:
    cluster = KVCluster(NODES)
    for i in range(100):
        cluster.put(f"item:{i}", f"v{i}")
    left, right = cluster.preference_list("item:3")[:2]
    assert cluster.anti_entropy(left, right).buckets == ()
    cluster.fail(right)
    cluster.put("item:3", "v3-new", context=cluster.get("item:3").context)
    for name in NODES:
        cluster.node(name).hints.clear()  # the hint is lost
    cluster.recover(right)
    sync = cluster.anti_entropy(left, right)
    assert len(sync.buckets) == 1 and "item:3" in sync.keys_synced
    assert sync.comparisons < 100
    assert cluster.node(right).data["item:3"][0].value == "v3-new"
    assert cluster.anti_entropy(left, right).buckets == ()


def test_tombstones_hide_values_and_validation() -> None:
    cluster = KVCluster(NODES)
    cluster.put("k", "v")
    cluster.delete("k", context=cluster.get("k").context)
    assert cluster.get("k").values == ()
    with pytest.raises(ValidationError):
        KVCluster(NODES, n=6)
    with pytest.raises(ValidationError):
        KVCluster(NODES, n=3, w=4)
    with pytest.raises(ValidationError):
        KVCluster(["A", "A"])
    with pytest.raises(ValidationError):
        cluster.put("k", "v", via="nope")
    assert KVCluster(NODES, n=3, w=1, r=1).overlapping is False


def test_concurrent_puts_and_gets_from_threads() -> None:
    cluster = KVCluster(NODES, n=3, w=2, r=2)

    def worker(i: int) -> tuple[str, ...]:
        key = f"key:{i % 50}"
        for step in range(5):
            ctx = cluster.get(key).context
            cluster.put(key, f"{i}-{step}", context=ctx)
        return cluster.get(key).values

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(200)))
    assert all(results)
    for i in range(50):
        key = f"key:{i}"
        assert cluster.holders(key) == sorted(cluster.preference_list(key))
        read = cluster.get(key)
        assert read.values and all(v.startswith(tuple(f"{j}-" for j in range(200))) for v in read.values)
