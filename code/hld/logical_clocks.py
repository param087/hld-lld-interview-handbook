"""Lamport clocks, vector clocks and hybrid logical clocks, with the conflicts they detect.

What the module demonstrates, in the order an interviewer asks about it:

* ``LamportClock`` is the scalar clock: every event ticks it, every message carries it, and a
  receiver jumps to ``max(local, stamp) + 1``. It guarantees ``a -> b`` implies ``L(a) < L(b)``
  and nothing in the other direction, so it orders events but cannot tell you they conflict.
* ``VectorClock`` keeps one counter per node, so ``compare`` returns BEFORE, AFTER, EQUAL or
  CONCURRENT. ``happens_before`` and ``concurrent`` are the two questions a replicated store
  actually asks, and CONCURRENT is the answer that means "two writes, keep both".
* ``VersionedStore`` keeps concurrent writes as siblings the way a Dynamo-style store does;
  ``LastWriterWinsStore`` keeps the higher timestamp and silently drops the other write.
* ``HybridLogicalClock`` glues a physical millisecond to a logical counter, so timestamps stay
  within a bounded distance of wall-clock time while still respecting causality.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from common import Clock, FakeClock, ValidationError


# --8<-- [start:lamport]
class LamportClock:
    """A scalar logical clock (Lamport 1978). ``_lock`` guards ``_time``.

    The rules are three lines long: tick before a local event, tick and attach the value to
    every outgoing message, and on receipt take ``max(local, received) + 1``. That buys exactly
    one property -- if ``a`` happened before ``b`` then ``L(a) < L(b)`` -- and deliberately not
    its converse: two unrelated events on different nodes also get ordered numbers, so a smaller
    stamp is no evidence of causality. Ties are broken by node id to get a total order.
    """

    def __init__(self, node_id: str, start: int = 0) -> None:
        if not node_id:
            raise ValidationError("node_id must be non-empty")
        if start < 0:
            raise ValidationError("start must be non-negative")
        self.node_id = node_id
        self._time = start
        self._lock = threading.Lock()

    @property
    def time(self) -> int:
        with self._lock:
            return self._time

    def tick(self) -> int:
        """A local event happened: advance and return the new time."""
        with self._lock:
            self._time += 1
            return self._time

    def send(self) -> int:
        """Stamp to attach to an outgoing message (a send is a local event)."""
        return self.tick()

    def receive(self, stamp: int) -> int:
        """A message arrived carrying ``stamp``: jump past it, then count the receive."""
        if stamp < 0:
            raise ValidationError("stamp must be non-negative")
        with self._lock:
            self._time = max(self._time, stamp) + 1
            return self._time

    def stamp(self) -> tuple[int, str]:
        """``(time, node_id)`` -- the tie-break that turns a partial order into a total one."""
        return (self.time, self.node_id)


# --8<-- [end:lamport]


# --8<-- [start:vector]
class Ordering(StrEnum):
    """The four possible relationships between two vector clocks."""

    BEFORE = "before"
    AFTER = "after"
    EQUAL = "equal"
    CONCURRENT = "concurrent"


@dataclass(frozen=True, slots=True)
class VectorClock:
    """One counter per node, stored as a sorted tuple so the value is immutable and hashable.

    Missing nodes count as zero, which is what lets a clock stay small: a node that has never
    written does not appear. ``tick`` and ``merge`` return new clocks rather than mutating, so a
    clock can be handed to a client as an opaque context and returned with the next write.
    """

    counters: tuple[tuple[str, int], ...] = ()

    @classmethod
    def of(cls, counters: Mapping[str, int]) -> VectorClock:
        for node, count in counters.items():
            if not node:
                raise ValidationError("node ids must be non-empty")
            if count < 0:
                raise ValidationError(f"counter for {node!r} must be non-negative")
        return cls(tuple(sorted((n, c) for n, c in counters.items() if c > 0)))

    def as_dict(self) -> dict[str, int]:
        return dict(self.counters)

    def get(self, node: str) -> int:
        return self.as_dict().get(node, 0)

    def tick(self, node: str) -> VectorClock:
        """The event's own node advances by one; every other counter is carried over."""
        counts = self.as_dict()
        counts[node] = counts.get(node, 0) + 1
        return VectorClock.of(counts)

    def merge(self, other: VectorClock) -> VectorClock:
        """Pointwise maximum: what a receiver knows after reading a message from a sender."""
        counts = self.as_dict()
        for node, count in other.counters:
            counts[node] = max(counts.get(node, 0), count)
        return VectorClock.of(counts)

    def compare(self, other: VectorClock) -> Ordering:
        """BEFORE, AFTER, EQUAL or CONCURRENT -- the whole point of carrying a vector."""
        mine, theirs = self.as_dict(), other.as_dict()
        nodes = set(mine) | set(theirs)
        less = any(mine.get(n, 0) < theirs.get(n, 0) for n in nodes)
        greater = any(mine.get(n, 0) > theirs.get(n, 0) for n in nodes)
        if less and greater:
            return Ordering.CONCURRENT
        if less:
            return Ordering.BEFORE
        if greater:
            return Ordering.AFTER
        return Ordering.EQUAL

    def happens_before(self, other: VectorClock) -> bool:
        """True when every counter is <= the other's and at least one is strictly smaller."""
        return self.compare(other) is Ordering.BEFORE

    def concurrent(self, other: VectorClock) -> bool:
        """True when neither clock dominates: two writes that never saw each other."""
        return self.compare(other) is Ordering.CONCURRENT

    def dominates(self, other: VectorClock) -> bool:
        """True when this clock has seen everything ``other`` has (AFTER or EQUAL)."""
        return self.compare(other) in (Ordering.AFTER, Ordering.EQUAL)

    def __str__(self) -> str:
        return "{" + ", ".join(f"{node}:{count}" for node, count in self.counters) + "}"


def happens_before(earlier: VectorClock, later: VectorClock) -> bool:
    """``earlier -> later`` in Lamport's happens-before relation."""
    return earlier.happens_before(later)


def concurrent(left: VectorClock, right: VectorClock) -> bool:
    """Neither clock dominates the other, so the two events conflict."""
    return left.concurrent(right)


# --8<-- [end:vector]


# --8<-- [start:store]
@dataclass(frozen=True, slots=True)
class Version:
    """One value and the vector clock it was written with."""

    value: str
    clock: VectorClock


class VersionedStore:
    """A register that keeps concurrent writes as siblings, the way Dynamo does.

    ``_lock`` guards ``_versions``. A write whose context dominates a stored sibling replaces it;
    a write concurrent with a sibling is kept beside it, and the *client* resolves the conflict
    on the next read. Nothing is ever silently discarded.
    """

    def __init__(self) -> None:
        self._versions: dict[str, tuple[Version, ...]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[Version, ...]:
        with self._lock:
            return self._versions.get(key, ())

    def put(self, key: str, value: str, node: str, context: VectorClock | None = None) -> Version:
        """Write ``value`` with the clock the client read (``context``), ticked for ``node``."""
        if not key or not node:
            raise ValidationError("key and node must be non-empty")
        with self._lock:
            siblings = self._versions.get(key, ())
            clock = (context or VectorClock()).tick(node)
            kept = tuple(v for v in siblings if not clock.dominates(v.clock))
            version = Version(value, clock)
            self._versions[key] = (*kept, version)
            return version

    def resolve(self, key: str, value: str, node: str) -> Version:
        """Collapse every sibling into one value whose clock is the merge of them all."""
        with self._lock:
            siblings = self._versions.get(key, ())
            merged = VectorClock()
            for sibling in siblings:
                merged = merged.merge(sibling.clock)
            version = Version(value, merged.tick(node))
            self._versions[key] = (version,)
            return version


class LastWriterWinsStore:
    """The same register with wall-clock last-writer-wins: the lower timestamp is dropped.

    ``_lock`` guards ``_values``. Ties go to the incumbent, which is why two writes stamped in
    the same millisecond by two nodes lose one of them whichever way you break the tie.
    """

    def __init__(self) -> None:
        self._values: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self.discarded = 0

    def put(self, key: str, value: str, timestamp_ms: float) -> bool:
        """Return True when the write was kept; False when an equal or newer one already won."""
        with self._lock:
            current = self._values.get(key)
            if current is not None and timestamp_ms <= current[0]:
                self.discarded += 1
                return False
            self._values[key] = (timestamp_ms, value)
            return True

    def get(self, key: str) -> str | None:
        with self._lock:
            current = self._values.get(key)
            return None if current is None else current[1]


# --8<-- [end:store]


# --8<-- [start:hlc]
@dataclass(frozen=True, slots=True, order=True)
class HybridTimestamp:
    """A physical millisecond plus a logical counter; compares lexicographically."""

    physical_ms: int
    logical: int

    def __str__(self) -> str:
        return f"{self.physical_ms}:{self.logical}"


class HybridLogicalClock:
    """HLC (Kulkarni et al. 2014): causality of a logical clock, readability of a physical one.

    ``_lock`` guards ``_last``. The logical counter only advances while physical time stands
    still, so a timestamp never drifts further from wall-clock time than the worst clock skew in
    the cluster -- which is why an HLC value is usable as an event time, and a Lamport counter
    is not. A remote timestamp further ahead than ``max_drift_ms`` is rejected rather than
    adopted, so one badly synchronised node cannot drag the cluster into the future.
    """

    def __init__(self, clock: Clock, max_drift_ms: int = 250) -> None:
        if max_drift_ms <= 0:
            raise ValidationError("max_drift_ms must be positive")
        self._clock = clock
        self._max_drift_ms = max_drift_ms
        self._last = HybridTimestamp(0, 0)
        self._lock = threading.Lock()

    def _physical_ms(self) -> int:
        return round(self._clock.now() * 1000)

    def now(self) -> HybridTimestamp:
        """Stamp a local event: use physical time if it moved, otherwise bump the counter."""
        with self._lock:
            physical = self._physical_ms()
            if physical > self._last.physical_ms:
                self._last = HybridTimestamp(physical, 0)
            else:
                self._last = HybridTimestamp(self._last.physical_ms, self._last.logical + 1)
            return self._last

    def update(self, remote: HybridTimestamp) -> HybridTimestamp:
        """Stamp the receipt of a message carrying ``remote``, keeping causality intact."""
        with self._lock:
            physical = self._physical_ms()
            if remote.physical_ms - physical > self._max_drift_ms:
                raise ValidationError(
                    f"remote clock {remote} is more than {self._max_drift_ms} ms ahead of {physical}"
                )
            newest = max(self._last.physical_ms, remote.physical_ms, physical)
            if newest == self._last.physical_ms == remote.physical_ms:
                logical = max(self._last.logical, remote.logical) + 1
            elif newest == self._last.physical_ms:
                logical = self._last.logical + 1
            elif newest == remote.physical_ms:
                logical = remote.logical + 1
            else:
                logical = 0
            self._last = HybridTimestamp(newest, logical)
            return self._last


# --8<-- [end:hlc]


def lamport_total_order(stamps: Iterable[tuple[int, str]]) -> list[tuple[int, str]]:
    """Sort ``(lamport_time, node_id)`` stamps: a total order consistent with causality."""
    return sorted(stamps)


def main() -> None:
    nodes = ["A", "B", "C"]
    lamport = {node: LamportClock(node) for node in nodes}
    vectors = {node: VectorClock() for node in nodes}
    rows: list[tuple[str, str, int, VectorClock]] = []

    def local(node: str, label: str) -> None:
        vectors[node] = vectors[node].tick(node)
        rows.append((node, label, lamport[node].tick(), vectors[node]))

    def send(src: str, dst: str, label: str) -> tuple[int, VectorClock]:
        vectors[src] = vectors[src].tick(src)
        stamp = lamport[src].send()
        rows.append((src, label, stamp, vectors[src]))
        return stamp, vectors[src]

    def deliver(dst: str, label: str, payload: tuple[int, VectorClock]) -> None:
        stamp, clock = payload
        vectors[dst] = vectors[dst].merge(clock).tick(dst)
        rows.append((dst, label, lamport[dst].receive(stamp), vectors[dst]))

    local("A", "a1 write x")
    m1 = send("A", "B", "a2 send m1")
    local("C", "c1 write x")
    deliver("B", "b1 recv m1", m1)
    local("B", "b2 write y")
    m2 = send("C", "A", "c2 send m2")
    deliver("A", "a3 recv m2", m2)

    print("three nodes, one message A->B and one C->A; Lamport stamp and vector clock per event")
    for node, label, stamp, clock in rows:
        print(f"  {node}  {label:<12} L={stamp:<3} V={clock}")

    b2 = next(clock for node, label, _, clock in rows if label.startswith("b2"))
    c1 = next(clock for node, label, _, clock in rows if label.startswith("c1"))
    a1 = next(clock for node, label, _, clock in rows if label.startswith("a1"))
    b1 = next(clock for node, label, _, clock in rows if label.startswith("b1"))
    print(
        f"b2 L=4 vs c1 L=1: Lamport says c1 is smaller, vectors say "
        f"{b2.compare(c1).value} (neither saw the other)"
    )
    print(f"a1 vs b1: happens_before={happens_before(a1, b1)}, concurrent={concurrent(a1, b1)}")

    print("two clients write cart:7 concurrently; A's clock runs 1 ms fast, B writes later")
    store = VersionedStore()
    socks = store.put("cart:7", "add socks", "A")
    shoes = store.put("cart:7", "add shoes", "B")
    siblings = store.get("cart:7")
    print(
        f"  vector clocks: {len(siblings)} siblings kept "
        f"{[v.value for v in siblings]}; concurrent={concurrent(socks.clock, shoes.clock)}"
    )
    merged = store.resolve("cart:7", "add socks + shoes", "A")
    print(f"  client merges -> {merged.value!r} with clock {merged.clock}")

    lww = LastWriterWinsStore()
    lww.put("cart:7", "add socks", 500_124.0)  # A stamps 500124 because its clock is fast
    kept = lww.put("cart:7", "add shoes", 500_123.0)  # B writes later but stamps 500123
    print(
        f"  last-writer-wins: kept={lww.get('cart:7')!r} (B's write accepted={kept}), "
        f"{lww.discarded} write silently discarded -- the shoes are gone"
    )

    clock_a, clock_b = FakeClock(start=500.000), FakeClock(start=499.998)
    hlc_a, hlc_b = HybridLogicalClock(clock_a), HybridLogicalClock(clock_b)
    first_ts = hlc_a.now()
    same_ms = hlc_a.now()
    received = hlc_b.update(same_ms)  # B's clock is 2 ms behind A's
    clock_b.advance(0.001)
    after = hlc_b.now()
    print(
        f"HLC: A stamps {first_ts} then {same_ms} in the same millisecond; "
        f"B is 2 ms behind, receives and stamps {received}, then {after}"
    )
    print(f"  causality holds: {same_ms < received < after}; B never rewinds below A's physical ms")


if __name__ == "__main__":
    main()
