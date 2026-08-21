"""A Dynamo-style key-value cluster: hash ring, N/W/R quorums, vector clocks, hinted handoff.

What the module demonstrates, in the order an interviewer asks about it:

* ``KVCluster`` places nodes on a ``HashRing`` with virtual nodes and stores each key on the
  N distinct nodes of its preference list (``hld.consistent_hashing``).
* ``put`` succeeds once W replicas hold the write and ``get`` merges the answers of R
  replicas; with W + R > N every read overlaps the latest write (``hld.quorum`` checks it).
* Every version carries a ``VectorClock``. The coordinator increments its own counter, a
  read returns every concurrent sibling plus a merged *context*, and a write that carries
  that context reconciles the siblings. Stale replicas a read touches are repaired in place.
* When a home replica is down, ``put`` writes to the next healthy node on the ring and keeps
  a hint (sloppy quorum); ``recover`` hands the hinted writes back (hinted handoff).
* ``anti_entropy`` compares two replicas' Merkle trees (``hld.merkle_tree``) and ships only
  the keys in the buckets that differ, which is how a lost hint is eventually repaired.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from common import NotFoundError, ValidationError
from hld.consistent_hashing import HashRing
from hld.merkle_tree import MerkleTree
from hld.quorum import QuorumError, quorum_overlaps


# --8<-- [start:vector_clock]
@dataclass(frozen=True, slots=True)
class VectorClock:
    """One counter per coordinator node, stored as sorted ``(node, counter)`` pairs.

    ``a.descends_from(b)`` means the write stamped ``a`` saw everything the write stamped
    ``b`` saw. When neither clock descends from the other the writes were concurrent, and
    the store keeps both versions as siblings instead of guessing a winner.
    """

    entries: tuple[tuple[str, int], ...] = ()

    def counter(self, node: str) -> int:
        return dict(self.entries).get(node, 0)

    def increment(self, node: str, seen: int = 0) -> VectorClock:
        """Bump ``node``'s counter past both this clock and ``seen``, the highest counter the
        coordinator already stores for the key, so a new version is never dominated by an
        older one the coordinator knows about."""
        counters = dict(self.entries)
        counters[node] = max(counters.get(node, 0), seen) + 1
        return VectorClock(tuple(sorted(counters.items())))

    def merge(self, other: VectorClock) -> VectorClock:
        """The smallest clock that descends from both: the context a reader hands back."""
        counters = dict(self.entries)
        for node, count in other.entries:
            counters[node] = max(counters.get(node, 0), count)
        return VectorClock(tuple(sorted(counters.items())))

    def descends_from(self, other: VectorClock) -> bool:
        return all(self.counter(node) >= count for node, count in other.entries)

    def dominates(self, other: VectorClock) -> bool:
        return self != other and self.descends_from(other)

    def concurrent_with(self, other: VectorClock) -> bool:
        return not self.descends_from(other) and not other.descends_from(self)

    def __str__(self) -> str:
        return "{" + ", ".join(f"{node}:{count}" for node, count in self.entries) + "}"


@dataclass(frozen=True, slots=True)
class Version:
    """A value and the clock of the write that produced it; ``None`` is a tombstone."""

    value: str | None
    clock: VectorClock


def reconcile(versions: Iterable[Version]) -> list[Version]:
    """Drop every version whose clock is dominated by another; what is left is concurrent.

    Two versions with equal clocks but different values (two writes from the same stale
    context through the same coordinator) are both kept rather than silently merged; dotted
    version vectors remove that corner case in production systems.
    """
    unique = sorted(set(versions), key=lambda v: (v.clock.entries, v.value or ""))
    return [v for v in unique if not any(o.clock.dominates(v.clock) for o in unique)]


# --8<-- [end:vector_clock]


# --8<-- [start:node]
@dataclass(slots=True)
class StorageNode:
    """One replica: its siblings per key, plus the hints it holds for nodes that were down.

    A production node is an LSM tree (``hld.lsm_tree``): WAL, memtable, SSTables. A dict
    keeps the replication logic readable here; the cluster lock guards every field.
    """

    name: str
    up: bool = True
    data: dict[str, list[Version]] = field(default_factory=dict)
    hints: dict[tuple[str, str], list[Version]] = field(default_factory=dict)  # (home, key)

    def store(self, key: str, *versions: Version) -> bool:
        """Merge ``versions`` into the key's siblings; True when the stored set changed."""
        merged = reconcile([*self.data.get(key, []), *versions])
        changed = merged != self.data.get(key)
        self.data[key] = merged
        return changed

    def fingerprint(self, keys: Iterable[str]) -> dict[str, str]:
        """``key -> canonical siblings`` for a Merkle tree: equal strings mean equal data."""
        return {
            key: "|".join(f"{v.value}@{v.clock}" for v in self.data[key])
            for key in keys
            if key in self.data
        }


# --8<-- [end:node]


@dataclass(frozen=True, slots=True)
class WriteResult:
    clock: VectorClock
    acked_by: tuple[str, ...]
    hinted: tuple[tuple[str, str], ...]  # (stand-in, home it holds a hint for)


@dataclass(frozen=True, slots=True)
class ReadResult:
    values: tuple[str, ...]  # one value normally; several when siblings exist
    context: VectorClock  # hand this back on the next put to reconcile the siblings
    answered_by: tuple[str, ...]
    repaired: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SyncResult:
    buckets: tuple[int, ...]
    comparisons: int
    keys_synced: tuple[str, ...]


# --8<-- [start:cluster]
class KVCluster:
    """Ring, quorum and versioning composed into one object.

    ``_ring`` decides where a key lives, ``_nodes`` hold the data. ``_lock`` guards every
    node's ``data``, ``hints`` and ``up`` flag; a real cluster locks per key on each node
    and talks over the network, but the quorum arithmetic is the same.
    """

    def __init__(
        self,
        nodes: Sequence[str],
        n: int = 3,
        w: int = 2,
        r: int = 2,
        vnodes: int = 64,
        sloppy: bool = True,
    ) -> None:
        if not nodes or len(set(nodes)) != len(nodes):
            raise ValidationError("nodes must be distinct and non-empty")
        if not 1 <= n <= len(nodes):
            raise ValidationError("need 1 <= N <= number of nodes")
        self.overlapping = quorum_overlaps(n, w, r)  # also validates 1 <= W, R <= N
        self.n, self.w, self.r, self.sloppy = n, w, r, sloppy
        self._ring = HashRing(nodes, vnodes=vnodes)
        self._nodes = {name: StorageNode(name) for name in nodes}
        self._lock = threading.RLock()

    def node(self, name: str) -> StorageNode:
        if name not in self._nodes:
            raise NotFoundError(f"no node named {name!r}")
        return self._nodes[name]

    def preference_list(self, key: str) -> list[str]:
        """The N distinct physical nodes clockwise from hash(key): the key's home replicas."""
        return self._ring.preference_list(key, self.n)

    def _targets(self, key: str) -> list[tuple[str, str]]:
        """``(home, holder)`` pairs: each home replica, or the next healthy node past the
        preference list standing in for a home that is down (a sloppy quorum)."""
        order = self._ring.preference_list(key, len(self._nodes))  # every node, ring order
        spare = (name for name in order[self.n :] if self._nodes[name].up)
        targets: list[tuple[str, str]] = []
        for home in order[: self.n]:
            if self._nodes[home].up:
                targets.append((home, home))
            elif self.sloppy and (stand_in := next(spare, None)) is not None:
                targets.append((home, stand_in))
        return targets

    def put(
        self, key: str, value: str | None, context: VectorClock | None = None, via: str | None = None
    ) -> WriteResult:
        """Write to the key's replicas through coordinator ``via`` (default: the first healthy
        one). Without a ``context`` the write is blind and becomes a sibling of anything
        concurrent; with the context of the last read it supersedes what that read saw."""
        with self._lock:
            targets = self._targets(key)
            holders = [holder for _, holder in targets]
            if len(holders) < self.w:
                raise QuorumError(f"write of {key!r}: only {len(holders)} replica(s) reachable, needed W={self.w}")
            coordinator = holders[0] if via is None else via
            if coordinator not in holders:
                raise ValidationError(f"{via!r} is not a healthy replica of {key!r}")
            local = self._nodes[coordinator].data.get(key, [])
            seen = max((v.clock.counter(coordinator) for v in local), default=0)
            version = Version(value, (context or VectorClock()).increment(coordinator, seen))
            hinted: list[tuple[str, str]] = []
            for home, holder in targets:
                node = self._nodes[holder]
                node.store(key, version)
                if holder != home:  # stand-in: serve reads now, hand the write back later
                    node.hints[(home, key)] = reconcile([*node.hints.get((home, key), []), version])
                    hinted.append((holder, home))
            return WriteResult(version.clock, tuple(holders), tuple(hinted))

    def delete(self, key: str, context: VectorClock | None = None) -> WriteResult:
        """A delete is the write of a tombstone: reads hide it, replication still ships it."""
        return self.put(key, None, context)

    def get(self, key: str) -> ReadResult:
        """Merge the siblings held by the first R healthy replicas and repair the stale ones."""
        with self._lock:
            answering = [holder for _, holder in self._targets(key)][: self.r]
            if len(answering) < self.r:
                raise QuorumError(f"read of {key!r} got {len(answering)} answers, needed R={self.r}")
            merged = reconcile(v for name in answering for v in self._nodes[name].data.get(key, []))
            repaired = tuple(name for name in answering if merged and self._nodes[name].store(key, *merged))
            context = VectorClock()
            for version in merged:
                context = context.merge(version.clock)
            values = tuple(v.value for v in merged if v.value is not None)
            return ReadResult(values, context, tuple(answering), repaired)

    def fail(self, name: str) -> None:
        with self._lock:
            self.node(name).up = False

    def recover(self, name: str) -> int:
        """Bring ``name`` back and deliver the hints other nodes kept for it (hinted handoff)."""
        with self._lock:
            node = self.node(name)
            node.up = True
            delivered = 0
            for other in self._nodes.values():
                for (home, key), versions in list(other.hints.items()):
                    if home != name:
                        continue
                    node.store(key, *versions)
                    del other.hints[(home, key)]
                    if other.name not in self.preference_list(key):
                        other.data.pop(key, None)  # the stand-in's copy has done its job
                    delivered += 1
            return delivered

    def anti_entropy(self, left: str, right: str, leaves: int = 64) -> SyncResult:
        """Compare the key range both nodes replicate via Merkle trees; sync only what differs."""
        with self._lock:
            a, b = self.node(left), self.node(right)
            shared = {k for k in (*a.data, *b.data) if {left, right} <= set(self.preference_list(k))}
            tree_a = MerkleTree(a.fingerprint(shared), leaves)
            tree_b = MerkleTree(b.fingerprint(shared), leaves)
            diff = tree_a.diff(tree_b)
            synced: list[str] = []
            for bucket in diff.buckets:
                for key in sorted({*tree_a.keys_in_bucket(bucket), *tree_b.keys_in_bucket(bucket)}):
                    merged = reconcile([*a.data.get(key, []), *b.data.get(key, [])])
                    a.store(key, *merged)
                    b.store(key, *merged)
                    synced.append(key)
            return SyncResult(diff.buckets, diff.comparisons, tuple(synced))

    def holders(self, key: str) -> list[str]:
        """Every node with a copy of ``key``, home replica or stand-in; for demos and tests."""
        with self._lock:
            return [name for name, node in self._nodes.items() if key in node.data]


# --8<-- [end:cluster]


def main() -> None:
    def say(step: str, outcome: str) -> None:
        print(f"{step:<34} -> {outcome}")

    key = "cart:42"
    cluster = KVCluster(["A", "B", "C", "D", "E"], n=3, w=2, r=2)
    homes = cluster.preference_list(key)
    print(f"5 nodes x 64 vnodes, N=3 W=2 R=2 (overlapping={cluster.overlapping}); {key} lives on {homes}")
    first = cluster.put(key, "apple")
    say("put 'apple', no context", f"clock {first.clock}, stored on {list(first.acked_by)}")
    read = cluster.get(key)
    say("get", f"{list(read.values)} context {read.context}")

    ctx = read.context
    left = cluster.put(key, "apple,bread", context=ctx, via=homes[1])
    right = cluster.put(key, "apple,milk", context=ctx, via=homes[2])
    say(f"two writes from {ctx} via {homes[1]} and {homes[2]}", f"clocks {left.clock} and {right.clock}")
    read = cluster.get(key)
    say("get", f"siblings {list(read.values)} context {read.context}")
    merged = cluster.put(key, "apple,bread,milk", context=read.context)
    read = cluster.get(key)
    say("client merges, writes with context", f"clock {merged.clock}; get -> {list(read.values)}")

    cluster.fail(homes[0])
    write = cluster.put(key, "apple,bread,milk,eggs", context=read.context)
    stand_in, home = write.hinted[0]
    say(f"{homes[0]} down, put", f"acks {list(write.acked_by)}; {stand_in} keeps a hint for {home}")
    read = cluster.get(key)
    say(f"get while {homes[0]} is down", f"{list(read.values)} answered by {list(read.answered_by)}")
    delivered = cluster.recover(homes[0])
    say(f"{homes[0]} recovers", f"{delivered} hinted write handed off, copies on {cluster.holders(key)}")

    strict = KVCluster(["A", "B", "C", "D", "E"], n=3, w=2, r=2, sloppy=False)
    for name in homes[:2]:
        strict.fail(name)
    try:
        strict.put(key, "x")
    except QuorumError as exc:
        say(f"strict quorum, {homes[0]} and {homes[1]} down", str(exc))

    for i in range(200):
        cluster.put(f"item:{i}", f"v{i}")
    pair = cluster.preference_list("item:7")[:2]
    cluster.fail(pair[1])
    cluster.put("item:7", "v7-updated", context=cluster.get("item:7").context)
    for node in cluster._nodes.values():
        node.hints.clear()  # the stand-in crashed before handing off: the hint is lost
    cluster.recover(pair[1])
    sync = cluster.anti_entropy(pair[0], pair[1])
    repaired = cluster.node(pair[1]).data["item:7"][0].value
    say(
        f"anti-entropy {pair[0]} vs {pair[1]}, lost hint",
        f"{len(sync.buckets)} bucket differs after {sync.comparisons} hash comparisons, "
        f"compared {list(sync.keys_synced)}; {pair[1]} now holds {repaired!r}",
    )


if __name__ == "__main__":
    main()
