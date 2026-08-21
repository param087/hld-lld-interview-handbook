from concurrent.futures import ThreadPoolExecutor

import pytest

from common import NotFoundError, ValidationError
from hld.quorum import Cluster, QuorumError, quorum_overlaps

NODES = ["A", "B", "C", "D", "E"]
KEY = "cart:42"


def homes(cluster: Cluster, key: str = KEY) -> list[str]:
    return [node.name for node in cluster.home_replicas(key)]


@pytest.mark.parametrize(
    ("n", "w", "r", "expected"),
    [(3, 1, 1, False), (3, 2, 2, True), (3, 1, 3, True), (3, 3, 1, True), (5, 2, 3, False), (5, 3, 3, True)],
)
def test_quorum_overlap_rule(n: int, w: int, r: int, expected: bool) -> None:
    assert quorum_overlaps(n, w, r) is expected


@pytest.mark.parametrize(("n", "w", "r"), [(3, 0, 1), (3, 4, 1), (3, 1, 0), (3, 1, 4)])
def test_quorum_overlap_rejects_bad_bounds(n: int, w: int, r: int) -> None:
    with pytest.raises(ValidationError):
        quorum_overlaps(n, w, r)


def test_put_reaches_every_healthy_home_replica_and_get_returns_newest() -> None:
    cluster = Cluster(NODES, n=3, w=2, r=2)
    assert len(homes(cluster)) == 3 and len(set(homes(cluster))) == 3
    assert cluster.put(KEY, "v1") == 1
    assert cluster.put(KEY, "v2") == 2
    assert cluster.versions(KEY) == dict.fromkeys(homes(cluster), 2)
    result = cluster.get(KEY)
    assert result.value is not None and result.value.value == "v2"
    assert len(result.answered_by) == 2 and result.repaired == ()
    assert cluster.get("never-written").value is None


def test_overlapping_quorum_reads_fresh_where_a_small_r_reads_stale() -> None:
    cluster = Cluster(NODES, n=3, w=2, r=2)
    first, second, third = homes(cluster)
    cluster.put(KEY, "apple")
    cluster.fail(third)
    cluster.put(KEY, "apple,bread")  # W=2 acks from the two healthy replicas
    assert cluster.versions(KEY) == {first: 2, second: 2, third: 1}
    cluster.recover(third)
    cluster.replica(third).delay_ms = 0.1  # the stale replica answers first
    stale = cluster.get(KEY, r=1)
    assert stale.value is not None and stale.value.value == "apple" and stale.answered_by == (third,)
    fresh = cluster.get(KEY, r=2)
    assert fresh.value is not None and fresh.value.value == "apple,bread"
    assert third in fresh.answered_by


def test_read_repair_fixes_only_the_replicas_the_read_touched() -> None:
    cluster = Cluster(NODES, n=3, w=1, r=3)
    first, second, third = homes(cluster)
    cluster.put(KEY, "v1")
    cluster.fail(second)
    cluster.fail(third)
    cluster.put(KEY, "v2")  # only the first replica has v2
    cluster.recover(second)
    cluster.recover(third)
    assert cluster.versions(KEY) == {first: 2, second: 1, third: 1}
    cluster.replica(third).delay_ms = 5.0  # too slow to make an R=2 quorum
    result = cluster.get(KEY, r=2)
    assert result.repaired == (second,)
    assert cluster.versions(KEY) == {first: 2, second: 2, third: 1}
    full = cluster.get(KEY, r=3)
    assert full.repaired == (third,)
    assert cluster.versions(KEY) == {first: 2, second: 2, third: 2}


def test_strict_quorum_fails_without_enough_healthy_replicas() -> None:
    cluster = Cluster(NODES, n=3, w=2, r=2)
    first, second, _ = homes(cluster)
    cluster.put(KEY, "v1")
    cluster.fail(first)
    cluster.fail(second)
    with pytest.raises(QuorumError):
        cluster.put(KEY, "v2")  # 1 ack < W, but the healthy replica keeps v2: no rollback
    with pytest.raises(QuorumError):
        cluster.get(KEY)
    partial = cluster.get(KEY, r=1)
    assert partial.value is not None and partial.value.value == "v2"  # a failed write is readable
    cluster.recover(first)
    assert cluster.put(KEY, "v3") == 3  # two healthy replicas again; the failed write used version 2


def test_sloppy_quorum_accepts_the_write_and_hands_hints_back_on_recovery() -> None:
    cluster = Cluster(NODES, n=3, w=2, r=2, sloppy=True)
    first, second, third = homes(cluster)
    cluster.fail(first)
    cluster.fail(second)
    assert cluster.put(KEY, "x") == 1
    holders = cluster.holders(KEY)
    assert third in holders and len(holders) == 3 and first not in holders
    assert cluster.get(KEY).value is not None  # stand-ins serve reads meanwhile
    assert cluster.recover(first) == 1
    assert cluster.replica(first).version_of(KEY) == 1
    assert cluster.recover(second) == 1
    assert cluster.recover(second) == 0  # hints are delivered once
    assert all(not node.hints for node in (cluster.replica(name) for name in NODES))
    assert cluster.versions(KEY) == {first: 1, second: 1, third: 1}


def test_construction_and_override_validation() -> None:
    with pytest.raises(ValidationError):
        Cluster(["A", "A"])
    with pytest.raises(ValidationError):
        Cluster(["A", "B"], n=3)
    with pytest.raises(ValidationError):
        Cluster(NODES, n=3, w=4)
    cluster = Cluster(NODES, n=3, w=2, r=2)
    with pytest.raises(ValidationError):
        cluster.put(KEY, "v", w=0)
    with pytest.raises(ValidationError):
        cluster.get(KEY, r=4)
    with pytest.raises(NotFoundError):
        cluster.replica("Z")


def test_concurrent_puts_and_reads_keep_versions_unique_and_visible() -> None:
    cluster = Cluster(NODES, n=3, w=2, r=2)

    def worker(worker_id: int) -> list[int]:
        versions: list[int] = []
        for i in range(50):
            key = f"k{worker_id}"
            versions.append(cluster.put(key, f"{worker_id}-{i}"))
            assert cluster.get(key).value is not None
        return versions

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    all_versions = [version for versions in results for version in versions]
    assert len(set(all_versions)) == 400 and max(all_versions) == 400
    for worker_id, versions in enumerate(results):
        result = cluster.get(f"k{worker_id}")
        assert result.value is not None and result.value.value == f"{worker_id}-49"
        assert result.value.version == versions[-1]
