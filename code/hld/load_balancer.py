"""Load balancer with pluggable strategies, passive outlier ejection and active health checks.

What the module demonstrates, in the order an interviewer asks about it:

* ``Balancer.pick`` routes a request to one of the *available* backends through a ``Strategy``:
  ``RoundRobin``, ``WeightedRoundRobin`` (nginx's smooth variant), ``LeastConnections`` and
  ``ConsistentHash`` (built on ``hld.consistent_hashing.HashRing``, so a key sticks to one
  backend and only that backend's keys move when it fails).
* ``Balancer.lease`` is the request lifecycle: it raises the backend's in-flight count, runs the
  request and reports the outcome, which is what least-connections and passive checks feed on.
* ``Balancer.report`` is the passive health check (outlier ejection): ``max_consecutive_errors``
  failures in a row eject a backend for ``ejection_seconds`` multiplied by its ejection count.
* ``Balancer.probe`` is the active health check: ``unhealthy_threshold`` failed probes mark a
  backend down, ``healthy_threshold`` passed probes bring it back.

Public API reused by the case studies: ``Backend``, ``HealthPolicy``, ``Strategy``, ``RoundRobin``,
``WeightedRoundRobin``, ``LeastConnections``, ``ConsistentHash``, ``Balancer``,
``NoAvailableBackend``. Every strategy is called under the balancer's lock, so strategies keep
their bookkeeping in plain attributes and never lock themselves.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from common import (
    Clock,
    ConflictError,
    FakeClock,
    InvalidStateError,
    NotFoundError,
    SystemClock,
    ValidationError,
)
from hld.consistent_hashing import HashRing


class NoAvailableBackend(InvalidStateError):
    """Every backend is down or ejected: answer 503 instead of routing."""


# --8<-- [start:backend]
@dataclass(frozen=True, slots=True)
class HealthPolicy:
    """Thresholds for active probes and passive outlier ejection (Envoy-style, scaled down)."""

    unhealthy_threshold: int = 2  # failed probes in a row before a backend is marked down
    healthy_threshold: int = 2  # passed probes in a row before it is marked up again
    max_consecutive_errors: int = 3  # request failures in a row before passive ejection
    ejection_seconds: float = 30.0  # base ejection time; the n-th ejection lasts n times this


@dataclass(slots=True)
class Backend:
    """One upstream server plus the health state the balancer keeps about it.

    ``healthy`` is owned by active probes, ``ejected_until`` by passive ejection; a backend
    takes traffic only when it is healthy *and* not ejected. Every field is mutated under the
    owning ``Balancer``'s lock, never by callers.
    """

    name: str
    weight: int = 1
    healthy: bool = True
    active: int = 0  # in-flight requests, maintained by ``Balancer.lease``
    probe_streak: int = 0  # > 0: probes passed in a row, < 0: probes failed in a row
    consecutive_errors: int = 0
    ejections: int = 0
    ejected_until: float | None = None

    def available(self, now: float) -> bool:
        return self.healthy and (self.ejected_until is None or now >= self.ejected_until)


# --8<-- [end:backend]


# --8<-- [start:strategies]
class Strategy(Protocol):
    """Picks one of the available backends; called with a non-empty list under the balancer lock."""

    def choose(self, backends: Sequence[Backend], key: str | None) -> Backend: ...


class RoundRobin:
    """Each available backend in turn; ignores weights and load."""

    def __init__(self) -> None:
        self._turn = 0

    def choose(self, backends: Sequence[Backend], key: str | None) -> Backend:
        backend = backends[self._turn % len(backends)]
        self._turn += 1
        return backend


class WeightedRoundRobin:
    """Nginx's smooth weighted round robin: weights 5:1:1 give A A B A C A A, never A A A A A B C.

    Every pick adds each backend's weight to its running score, takes the highest score and
    subtracts the total weight from the winner, so heavy backends are interleaved with light
    ones instead of receiving their whole share in a burst.
    """

    def __init__(self) -> None:
        self._score: dict[str, int] = {}

    def choose(self, backends: Sequence[Backend], key: str | None) -> Backend:
        total = sum(backend.weight for backend in backends)
        for backend in backends:
            self._score[backend.name] = self._score.get(backend.name, 0) + backend.weight
        best = max(backends, key=lambda backend: self._score[backend.name])
        self._score[best.name] -= total
        return best


class LeastConnections:
    """The backend with the fewest in-flight requests per unit of weight; ties broken by name."""

    def choose(self, backends: Sequence[Backend], key: str | None) -> Backend:
        return min(backends, key=lambda backend: (backend.active / backend.weight, backend.name))


class ConsistentHash:
    """Same key, same backend, for as long as that backend is available.

    The ring is rebuilt only when the available set (or a weight) changes. Losing a backend
    moves only its own keys, about 1/N of them, to their clockwise successors; IP hash mod N
    would remap almost everything. Requests without a key fall back to round robin.
    """

    def __init__(self, vnodes: int = 100) -> None:
        self._vnodes = vnodes
        self._members: tuple[tuple[str, int], ...] = ()
        self._ring = HashRing(vnodes=vnodes)
        self._fallback = RoundRobin()

    def choose(self, backends: Sequence[Backend], key: str | None) -> Backend:
        if key is None:
            return self._fallback.choose(backends, key)
        by_name = {backend.name: backend for backend in backends}
        members = tuple(sorted((backend.name, backend.weight) for backend in backends))
        if members != self._members:
            self._ring = HashRing(vnodes=self._vnodes)
            for name, weight in members:
                self._ring.add_node(name, weight)
            self._members = members
        return by_name[self._ring.get_node(key)]


# --8<-- [end:strategies]


# --8<-- [start:balancer]
class Balancer:
    """Routes requests to available backends and owns their health state.

    ``_lock`` guards the backend table, every ``Backend`` field and the strategy's internal
    state: strategies run only while it is held. It is a plain ``Lock``, so the public methods
    never call each other while holding it; they share the underscore helpers instead.
    """

    def __init__(
        self,
        backends: Iterable[Backend],
        strategy: Strategy,
        clock: Clock | None = None,
        policy: HealthPolicy | None = None,
    ) -> None:
        self._backends: dict[str, Backend] = {}
        self._strategy = strategy
        self._clock = clock or SystemClock()
        self._policy = policy or HealthPolicy()
        self._lock = threading.Lock()
        for backend in backends:
            self.add(backend)

    def add(self, backend: Backend) -> None:
        if backend.weight <= 0:
            raise ValidationError(f"backend {backend.name!r} needs a positive weight")
        with self._lock:
            if backend.name in self._backends:
                raise ConflictError(f"backend {backend.name!r} is already in the pool")
            self._backends[backend.name] = backend

    def remove(self, name: str) -> None:
        with self._lock:
            self._get(name)
            del self._backends[name]

    def available(self) -> list[str]:
        """Names of the backends that would receive traffic right now."""
        with self._lock:
            return [backend.name for backend in self._available()]

    def pick(self, key: str | None = None) -> Backend:
        """Choose a backend without touching its connection count (stateless routing)."""
        with self._lock:
            return self._pick(key)

    @contextmanager
    def lease(self, key: str | None = None) -> Iterator[Backend]:
        """Pick a backend, hold one in-flight request on it for the block, report the outcome.

        An exception escaping the block counts as a failure (connection error, 5xx); a
        normal exit counts as a success. A real proxy would not count 4xx responses.
        """
        with self._lock:
            backend = self._pick(key)
            backend.active += 1
        ok = False
        try:
            yield backend
            ok = True
        finally:
            with self._lock:
                backend.active -= 1
                self._report(backend, ok)

    def report(self, name: str, ok: bool) -> None:
        """Passive health check: an outcome observed on real traffic."""
        with self._lock:
            self._report(self._get(name), ok)

    def probe(self, name: str, ok: bool) -> None:
        """Active health check: the result of a timed GET /healthz or TCP connect."""
        with self._lock:
            backend = self._get(name)
            streak = backend.probe_streak
            backend.probe_streak = max(1, streak + 1) if ok else min(-1, streak - 1)
            if backend.healthy and backend.probe_streak <= -self._policy.unhealthy_threshold:
                backend.healthy = False
            elif not backend.healthy and backend.probe_streak >= self._policy.healthy_threshold:
                backend.healthy = True

    def status(self, name: str) -> str:
        """'healthy', 'unhealthy' or 'ejected for Ns' for dashboards and demos."""
        with self._lock:
            backend = self._get(name)
            now = self._clock.now()
            if not backend.healthy:
                return "unhealthy"
            if backend.ejected_until is not None and now < backend.ejected_until:
                return f"ejected for {backend.ejected_until - now:.0f}s"
            return "healthy"

    def _get(self, name: str) -> Backend:
        try:
            return self._backends[name]
        except KeyError:
            raise NotFoundError(f"backend {name!r} is not in the pool") from None

    def _available(self) -> list[Backend]:
        now = self._clock.now()
        candidates: list[Backend] = []
        for backend in self._backends.values():
            if backend.ejected_until is not None and now >= backend.ejected_until:
                backend.ejected_until = None  # ejection served: a clean slate
                backend.consecutive_errors = 0
            if backend.available(now):
                candidates.append(backend)
        return candidates

    def _pick(self, key: str | None) -> Backend:
        candidates = self._available()
        if not candidates:
            raise NoAvailableBackend("no healthy, non-ejected backend in the pool")
        return self._strategy.choose(candidates, key)

    def _report(self, backend: Backend, ok: bool) -> None:
        if ok:
            backend.consecutive_errors = 0
            return
        backend.consecutive_errors += 1
        if backend.consecutive_errors < self._policy.max_consecutive_errors:
            return
        if backend.ejected_until is None:
            backend.ejections += 1
            backend.ejected_until = (
                self._clock.now() + self._policy.ejection_seconds * backend.ejections
            )


# --8<-- [end:balancer]


def main() -> None:
    clock = FakeClock(start=1_000.0)

    def pool(*weights: int) -> list[Backend]:
        return [Backend(name, weight=w) for name, w in zip("ABC", weights, strict=True)]

    rr = Balancer(pool(1, 1, 1), RoundRobin(), clock)
    print("round robin         :", " ".join(rr.pick().name for _ in range(8)))
    wrr = Balancer(pool(5, 1, 1), WeightedRoundRobin(), clock)
    print("smooth weighted 5:1:1:", " ".join(wrr.pick().name for _ in range(14)))

    busy = [Backend("A", active=3), Backend("B", active=1), Backend("C")]
    lc = Balancer(busy, LeastConnections(), clock)
    with lc.lease() as first, lc.lease() as second:
        print(f"least connections   : active A=3 B=1 C=0 -> {first.name}, then {second.name}")

    ch = Balancer(pool(1, 1, 1), ConsistentHash(), clock)
    keys = [f"user:{i}" for i in range(1_000)]
    before = {key: ch.pick(key).name for key in keys}
    spread = " ".join(f"{name}={count}" for name, count in sorted(Counter(before.values()).items()))
    print(f"consistent hash     : 1,000 keys -> {spread}")

    for _ in range(3):
        ch.report("B", ok=False)
    after = {key: ch.pick(key).name for key in keys}
    moved = sum(1 for key in keys if before[key] != after[key])
    held_by_b = sum(1 for owner in before.values() if owner == "B")
    print(f"B fails 3 requests  : {ch.status('B')}; available = {ch.available()}")
    print(f"keys that moved     : {moved} of 1,000 = exactly B's {held_by_b} (A and C keys stayed)")
    clock.advance(30)
    returned = sum(1 for key in keys if ch.pick(key).name == before[key])
    print(f"30 s later          : B is {ch.status('B')}; {returned} of 1,000 keys back on their owner")
    for _ in range(3):
        ch.report("B", ok=False)
    print(f"B fails 3 more      : {ch.status('B')} (second ejection lasts twice as long)")

    ch.probe("C", ok=False)
    print(f"active probe on C   : 1 failure -> {ch.status('C')} (threshold is 2)")
    ch.probe("C", ok=False)
    print(f"active probe on C   : 2 failures -> {ch.status('C')}; available = {ch.available()}")
    ch.probe("C", ok=True)
    ch.probe("C", ok=True)
    print(f"active probe on C   : 2 passes -> {ch.status('C')}; available = {ch.available()}")

    for _ in range(3):
        ch.report("A", ok=False)
        ch.report("C", ok=False)
    try:
        ch.pick("user:1")
    except NoAvailableBackend as exc:
        print(f"A and C ejected too : NoAvailableBackend, answer 503 ({exc})")


if __name__ == "__main__":
    main()
