"""Consistent hashing ring with virtual nodes, replication and key-movement statistics.

What the module demonstrates, in the order an interviewer asks about it:

* ``HashRing`` places every node at ``vnodes`` pseudo-random positions on a 2^32 ring and
  routes a key to the first position clockwise from ``hash(key)`` with a ``bisect`` lookup.
* ``add_node`` / ``remove_node`` change membership; ``keys_moved`` measures how many keys
  change owner (about 1/N for the ring versus about N/(N+1) for ``hash mod N``).
* ``preference_list`` walks clockwise to the next *distinct* physical nodes, which is how
  Dynamo-style stores pick the N replicas of a key.
* ``load_stats`` shows why virtual nodes matter: one point per node leaves some nodes with
  about twice the average load; a hundred points per node evens it out.
"""

from __future__ import annotations

import bisect
import hashlib
import threading
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError

# --8<-- [start:hashing]
RING_BITS = 32
RING_SIZE = 1 << RING_BITS


def ring_hash(key: str) -> int:
    """Position of ``key`` on the ring: the first 32 bits of its MD5 digest.

    The hash must be *stable* across processes and languages, because every client has to
    compute the same ring. Python's built-in ``hash()`` is salted per process, so it is
    exactly the wrong tool. MD5 is what Ketama uses; MurmurHash3 or xxHash are faster
    production choices and equally fine (spread matters, cryptographic strength does not).
    """
    digest = hashlib.md5(key.encode(), usedforsecurity=False).digest()
    return int.from_bytes(digest[:4], "big")


# --8<-- [end:hashing]


# --8<-- [start:ring]
class HashRing:
    """A consistent-hash ring with virtual nodes.

    ``_ring`` is an immutable, sorted tuple of ``(position, node)`` pairs. ``_lock``
    serialises membership changes, which rebuild the tuple and swap it in (copy-on-write),
    and guards ``_weights``; lookups read the current tuple without locking, because
    lookups outnumber topology changes by many orders of magnitude. Ties on ``position``
    are broken by node name, so the ring is a pure function of the member set: every
    client computes the same ring whatever the order in which it learned about the nodes.
    """

    def __init__(self, nodes: Iterable[str] = (), vnodes: int = 100) -> None:
        if vnodes <= 0:
            raise ValidationError("vnodes must be positive")
        self._vnodes = vnodes
        self._ring: tuple[tuple[int, str], ...] = ()
        self._weights: dict[str, int] = {}
        self._lock = threading.Lock()
        for node in nodes:
            self.add_node(node)

    def __len__(self) -> int:
        """Number of points (virtual nodes) on the ring."""
        return len(self._ring)

    @property
    def vnodes(self) -> int:
        return self._vnodes

    @property
    def nodes(self) -> list[str]:
        with self._lock:
            return sorted(self._weights)

    @staticmethod
    def _points(node: str, count: int) -> list[tuple[int, str]]:
        return [(ring_hash(f"{node}#{i}"), node) for i in range(count)]

    def add_node(self, node: str, weight: int = 1) -> None:
        """Place ``vnodes * weight`` points for ``node``: a box with 2x capacity gets 2x the ring."""
        if not node:
            raise ValidationError("node name must be non-empty")
        if weight <= 0:
            raise ValidationError("weight must be positive")
        with self._lock:
            if node in self._weights:
                raise ConflictError(f"node {node!r} is already on the ring")
            points = self._points(node, self._vnodes * weight)
            self._ring = tuple(sorted(self._ring + tuple(points)))
            self._weights[node] = weight

    def remove_node(self, node: str) -> None:
        """Drop every point of ``node``; its keys fall to their clockwise successors."""
        with self._lock:
            if node not in self._weights:
                raise NotFoundError(f"node {node!r} is not on the ring")
            self._ring = tuple(point for point in self._ring if point[1] != node)
            del self._weights[node]

    def get_node(self, key: str) -> str:
        """Owner of ``key``: the first point clockwise from ``hash(key)``, O(log V) by bisect."""
        ring = self._ring
        if not ring:
            raise InvalidStateError("ring has no nodes")
        idx = bisect.bisect_left(ring, (ring_hash(key), ""))
        return ring[idx % len(ring)][1]

    def preference_list(self, key: str, replicas: int = 3) -> list[str]:
        """The first ``replicas`` *distinct* physical nodes clockwise from ``hash(key)``.

        This is Dynamo's preference list: the owner plus the next N-1 nodes hold the key's
        replicas, skipping further virtual nodes of a physical node already chosen.
        """
        if replicas <= 0:
            raise ValidationError("replicas must be positive")
        ring = self._ring
        if not ring:
            raise InvalidStateError("ring has no nodes")
        start = bisect.bisect_left(ring, (ring_hash(key), ""))
        owners: list[str] = []
        for offset in range(len(ring)):
            node = ring[(start + offset) % len(ring)][1]
            if node not in owners:
                owners.append(node)
                if len(owners) == replicas:
                    return owners
        raise ValidationError(f"cannot place {replicas} replicas on {len(owners)} nodes")


# --8<-- [end:ring]


# --8<-- [start:stats]
@dataclass(frozen=True, slots=True)
class MoveStats:
    """How many keys changed owner between two placements of the same key set."""

    total: int
    moved: int

    @property
    def fraction(self) -> float:
        return self.moved / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class LoadStats:
    """Keys per node and the peak-to-mean ratio (1.0 is perfect balance)."""

    per_node: dict[str, int]
    peak_to_mean: float


def assignments(ring: HashRing, keys: Iterable[str]) -> dict[str, str]:
    """``key -> node`` for every key, so two rings (or two moments) can be compared."""
    return {key: ring.get_node(key) for key in keys}


def mod_assignments(nodes: Sequence[str], keys: Iterable[str]) -> dict[str, str]:
    """The naive scheme, ``node = hash(key) mod N``: adding one node remaps most keys."""
    return {key: nodes[ring_hash(key) % len(nodes)] for key in keys}


def keys_moved(before: Mapping[str, str], after: Mapping[str, str]) -> MoveStats:
    moved = sum(1 for key, node in before.items() if after.get(key) != node)
    return MoveStats(total=len(before), moved=moved)


def load_stats(assignment: Mapping[str, str], nodes: Iterable[str]) -> LoadStats:
    counts = Counter(assignment.values())
    per_node = {node: counts.get(node, 0) for node in nodes}
    if not per_node:
        raise ValidationError("no nodes to compute load for")
    mean = len(assignment) / len(per_node)
    peak = max(per_node.values())
    return LoadStats(per_node=per_node, peak_to_mean=peak / mean if mean else 0.0)


# --8<-- [end:stats]


def main() -> None:
    keys = [f"user:{i}" for i in range(10_000)]
    nodes = ["A", "B", "C", "D"]
    ring = HashRing(nodes, vnodes=100)
    print(f"ring: {len(nodes)} nodes x {ring.vnodes} vnodes = {len(ring)} points; {len(keys):,} keys")

    def show_load(label: str, sample: HashRing) -> None:
        stats = load_stats(assignments(sample, keys), sample.nodes)
        per_node = " ".join(f"{node}={count:,}" for node, count in stats.per_node.items())
        print(f"{label:<18} {per_node}  peak/mean={stats.peak_to_mean:.2f}")

    show_load("load, vnodes=1:", HashRing(nodes, vnodes=1))
    show_load("load, vnodes=100:", ring)
    before = assignments(ring, keys)
    print(f"preference list for user:42 (N=3): {ring.preference_list('user:42', 3)}")

    ring.add_node("E")
    after_add = assignments(ring, keys)
    moved = keys_moved(before, after_add)
    landed_on_e = sum(
        1 for key, node in before.items() if after_add[key] != node and after_add[key] == "E"
    )
    naive = keys_moved(mod_assignments(nodes, keys), mod_assignments([*nodes, "E"], keys))
    print(
        f"add E, ring:   {moved.moved:,}/{moved.total:,} keys moved = {moved.fraction:.1%}, "
        f"{landed_on_e:,} of them onto E (expected ~1/5 = 20%)"
    )
    print(
        f"add E, mod N:  {naive.moved:,}/{naive.total:,} keys moved = {naive.fraction:.1%} "
        f"(expected ~4/5 = 80%)"
    )

    held_by_b = sum(1 for node in after_add.values() if node == "B")
    ring.remove_node("B")
    after_remove = assignments(ring, keys)
    moved = keys_moved(after_add, after_remove)
    print(
        f"remove B:      {moved.moved:,} keys moved = {moved.fraction:.1%}; "
        f"B held {held_by_b:,} (expected ~1/5 = 20%)"
    )
    print(f"preference list for user:42 after: {ring.preference_list('user:42', 3)}")


if __name__ == "__main__":
    main()
