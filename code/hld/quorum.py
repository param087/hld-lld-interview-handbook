"""Quorum reads and writes over N replicas: N/W/R, sloppy quorums, hinted handoff, read repair.

What the module demonstrates, in the order an interviewer asks about it:

* ``Cluster.put`` sends a versioned value to a key's N home replicas and succeeds once W of
  them acknowledge; ``Cluster.get`` returns the newest of the first R answers.
* ``W + R > N`` makes every read quorum overlap every write quorum, so a read always sees the
  newest acknowledged write. ``W + R <= N`` allows the stale read the demo shows.
* A sloppy quorum writes to the next healthy node when a home replica is down and keeps a hint;
  ``recover`` hands the hinted writes back (hinted handoff).
* Read repair writes the newest version back to the stale replicas a read happened to touch.

Versions are a single increasing stamp (last-writer-wins). Production systems use wall-clock
microseconds (Cassandra) or vector clocks (Dynamo, Riak); the quorum arithmetic is the same.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

from common import HandbookError, NotFoundError, ValidationError


class QuorumError(HandbookError):
    """Fewer than W replicas acknowledged a write, or fewer than R answered a read."""


# --8<-- [start:model]
@dataclass(frozen=True, slots=True)
class Versioned:
    """A value with its last-writer-wins stamp; the highest version is the newest."""

    value: str
    version: int


@dataclass(slots=True)
class Replica:
    """One storage node. ``delay_ms`` orders the answers a read receives: a read takes the R
    fastest, so a slow replica may be left out of the read quorum entirely."""

    name: str
    up: bool = True
    delay_ms: float = 1.0
    data: dict[str, Versioned] = field(default_factory=dict)
    hints: dict[tuple[str, str], Versioned] = field(default_factory=dict)  # (home, key) -> value

    def version_of(self, key: str) -> int | None:
        item = self.data.get(key)
        return None if item is None else item.version


@dataclass(frozen=True, slots=True)
class ReadResult:
    """What a quorum read returned and which replicas it repaired on the way."""

    value: Versioned | None
    answered_by: tuple[str, ...]
    repaired: tuple[str, ...]


def quorum_overlaps(n: int, w: int, r: int) -> bool:
    """True when every read quorum shares at least one replica with every write quorum."""
    if not 1 <= w <= n or not 1 <= r <= n:
        raise ValidationError("need 1 <= W <= N and 1 <= R <= N")
    return w + r > n


# --8<-- [end:model]


# --8<-- [start:cluster]
class Cluster:
    """A ring of replicas with tunable N, W and R (per request, as Cassandra allows).

    The home replicas of a key are the N nodes clockwise from ``hash(key)``. ``_lock``
    guards every replica's ``data``, ``hints`` and ``up`` flag, and the version counter.
    """

    def __init__(
        self, nodes: Sequence[str], n: int = 3, w: int = 2, r: int = 2, sloppy: bool = False
    ) -> None:
        if len(nodes) != len(set(nodes)) or not nodes:
            raise ValidationError("nodes must be distinct and non-empty")
        if not 1 <= n <= len(nodes):
            raise ValidationError("need 1 <= N <= number of nodes")
        quorum_overlaps(n, w, r)  # validates the W and R bounds
        self._nodes = [Replica(name) for name in nodes]
        self.n, self.w, self.r, self.sloppy = n, w, r, sloppy
        self._next_version = 0
        self._lock = threading.Lock()

    def replica(self, name: str) -> Replica:
        for node in self._nodes:
            if node.name == name:
                return node
        raise NotFoundError(f"no replica named {name!r}")

    def home_replicas(self, key: str) -> list[Replica]:
        """The key's preference list: N consecutive nodes starting at hash(key)."""
        start = int.from_bytes(hashlib.md5(key.encode(), usedforsecurity=False).digest()[:4], "big")
        return [self._nodes[(start + i) % len(self._nodes)] for i in range(self.n)]

    def _substitute(self, key: str, taken: set[str]) -> Replica | None:
        """Next healthy node clockwise beyond the home replicas, for a sloppy-quorum write."""
        start = self._nodes.index(self.home_replicas(key)[-1]) + 1
        for i in range(len(self._nodes)):
            node = self._nodes[(start + i) % len(self._nodes)]
            if node.up and node.name not in taken:
                return node
        return None

    def put(self, key: str, value: str, w: int | None = None) -> int:
        """Write to the home replicas; succeed once ``w`` acknowledge. Returns the version."""
        w = self.w if w is None else w
        quorum_overlaps(self.n, w, self.r)
        with self._lock:
            self._next_version += 1
            item = Versioned(value, self._next_version)
            acks: set[str] = set()
            for home in self.home_replicas(key):
                if home.up:
                    home.data[key] = item
                    acks.add(home.name)
                elif self.sloppy and (stand_in := self._substitute(key, acks)) is not None:
                    stand_in.data[key] = item  # serves reads meanwhile ...
                    stand_in.hints[(home.name, key)] = item  # ... and is handed back later
                    acks.add(stand_in.name)
            if len(acks) < w:
                raise QuorumError(f"write of {key!r} got {len(acks)} acks, needed W={w}")
            return item.version

    def get(self, key: str, r: int | None = None) -> ReadResult:
        """Read from the ``r`` fastest healthy home replicas, return the newest, repair the rest."""
        r = self.r if r is None else r
        quorum_overlaps(self.n, self.w, r)
        with self._lock:
            healthy = [node for node in self.home_replicas(key) if node.up]
            if self.sloppy and len(healthy) < self.n:
                taken = {node.name for node in healthy}
                while len(healthy) < self.n and (extra := self._substitute(key, taken)) is not None:
                    healthy.append(extra)
                    taken.add(extra.name)
            answering = sorted(healthy, key=lambda node: node.delay_ms)[:r]
            if len(answering) < r:
                raise QuorumError(f"read of {key!r} got {len(answering)} answers, needed R={r}")
            found = [node.data[key] for node in answering if key in node.data]
            newest = max(found, key=lambda item: item.version, default=None)
            repaired: list[str] = []
            for node in answering:
                if newest is not None and node.data.get(key) != newest:
                    node.data[key] = newest  # read repair
                    repaired.append(node.name)
            return ReadResult(newest, tuple(node.name for node in answering), tuple(repaired))

    def fail(self, name: str) -> None:
        with self._lock:
            self.replica(name).up = False

    def recover(self, name: str) -> int:
        """Bring a node back and deliver the hints other nodes kept for it (hinted handoff)."""
        with self._lock:
            node = self.replica(name)
            node.up = True
            delivered = 0
            for other in self._nodes:
                for (home, key), item in list(other.hints.items()):
                    if home == name:
                        current = node.data.get(key)
                        if current is None or current.version < item.version:
                            node.data[key] = item
                        del other.hints[(home, key)]
                        delivered += 1
            return delivered

    def versions(self, key: str) -> dict[str, int | None]:
        """Which version each home replica holds, for demos and tests."""
        with self._lock:
            return {node.name: node.version_of(key) for node in self.home_replicas(key)}

    def holders(self, key: str) -> list[str]:
        """Every node holding a copy of ``key``, home replica or sloppy-quorum stand-in."""
        with self._lock:
            return [node.name for node in self._nodes if key in node.data]


# --8<-- [end:cluster]


def main() -> None:
    key = "cart:42"
    cluster = Cluster(["A", "B", "C", "D", "E"], n=3, w=2, r=2)
    homes = [node.name for node in cluster.home_replicas(key)]
    print(f"N=3 W=2 R=2 over {len(['A', 'B', 'C', 'D', 'E'])} nodes; home replicas of {key}: {homes}")
    cluster.put(key, "apple")
    print(f"put v1 'apple'              -> versions {cluster.versions(key)}")
    cluster.fail(homes[2])
    cluster.put(key, "apple,bread")
    print(f"{homes[2]} down, put v2 'apple,bread' -> versions {cluster.versions(key)} (W=2 acks were enough)")
    cluster.recover(homes[2])
    cluster.replica(homes[2]).delay_ms = 0.1  # the stale replica is now the fastest to answer
    stale = cluster.get(key, r=1)
    assert stale.value is not None
    print(
        f"{homes[2]} back and fastest; read with R=1 -> {stale.value.value!r} v{stale.value.version} "
        f"from {stale.answered_by}: STALE, W+R = 3 is not > N"
    )
    fresh = cluster.get(key, r=2)
    assert fresh.value is not None
    print(
        f"read with R=2 -> {fresh.value.value!r} v{fresh.value.version} from {fresh.answered_by}, "
        f"read repair fixed {fresh.repaired}"
    )
    print(f"after read repair           -> versions {cluster.versions(key)}")

    strict = Cluster(["A", "B", "C", "D", "E"], n=3, w=2, r=2)
    sloppy = Cluster(["A", "B", "C", "D", "E"], n=3, w=2, r=2, sloppy=True)
    for node in homes[:2]:
        strict.fail(node)
        sloppy.fail(node)
    try:
        strict.put(key, "x")
    except QuorumError as exc:
        print(f"strict quorum, {homes[0]} and {homes[1]} down: {exc}")
    sloppy.put(key, "x")
    print(f"sloppy quorum, same outage: write accepted, copies on {sloppy.holders(key)} (hints for {homes[:2]})")
    delivered = sloppy.recover(homes[0])
    print(f"{homes[0]} recovers: {delivered} hinted write handed off, {homes[0]} now holds v{sloppy.replica(homes[0]).version_of(key)}")
    print("overlap table: " + ", ".join(
        f"N={n} W={w} R={r}: {'yes' if quorum_overlaps(n, w, r) else 'no'}"
        for n, w, r in [(3, 1, 1), (3, 2, 2), (3, 1, 3), (3, 3, 1), (5, 2, 3)]
    ))


if __name__ == "__main__":
    main()
