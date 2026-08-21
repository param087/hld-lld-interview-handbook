from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, FakeClock, NotFoundError, ValidationError
from hld.load_balancer import (
    Backend,
    Balancer,
    ConsistentHash,
    HealthPolicy,
    LeastConnections,
    NoAvailableBackend,
    RoundRobin,
    WeightedRoundRobin,
)

KEYS = [f"user:{i}" for i in range(2_000)]


def pool(*weights: int) -> list[Backend]:
    return [Backend(name, weight=w) for name, w in zip("ABCD"[: len(weights)], weights, strict=True)]


def test_round_robin_cycles_and_skips_unavailable_backends() -> None:
    clock = FakeClock()
    lb = Balancer(pool(1, 1, 1), RoundRobin(), clock)
    assert [lb.pick().name for _ in range(6)] == ["A", "B", "C", "A", "B", "C"]
    lb.probe("B", ok=False)
    lb.probe("B", ok=False)
    assert lb.available() == ["A", "C"]
    assert set(lb.pick().name for _ in range(4)) == {"A", "C"}


def test_smooth_weighted_round_robin_interleaves_heavy_and_light_backends() -> None:
    lb = Balancer(pool(5, 1, 1), WeightedRoundRobin(), FakeClock())
    picks = [lb.pick().name for _ in range(14)]
    assert picks[:7] == ["A", "A", "B", "A", "C", "A", "A"]
    assert picks[7:] == picks[:7]  # the cycle repeats with the same interleaving
    assert picks.count("A") == 10 and picks.count("B") == 2 and picks.count("C") == 2


def test_least_connections_follows_in_flight_counts_and_weights() -> None:
    lb = Balancer([Backend("A", active=3), Backend("B", active=1), Backend("C")], LeastConnections(), FakeClock())
    with lb.lease() as first, lb.lease() as second:
        assert (first.name, second.name) == ("C", "B")
        assert first.active == 1 and second.active == 2
    assert first.active == 0 and second.active == 1  # seeded count stays, lease is released
    weighted = Balancer([Backend("big", weight=4, active=4), Backend("small", active=2)], LeastConnections(), FakeClock())
    assert weighted.pick().name == "big"  # 4/4 = 1 in flight per weight unit beats 2/1


def test_consistent_hash_is_sticky_and_moves_only_the_failed_backends_keys() -> None:
    clock = FakeClock()
    lb = Balancer(pool(1, 1, 1), ConsistentHash(vnodes=50), clock)
    before = {key: lb.pick(key).name for key in KEYS}
    assert before == {key: lb.pick(key).name for key in KEYS}
    assert len(set(before.values())) == 3
    for _ in range(3):
        lb.report("B", ok=False)
    after = {key: lb.pick(key).name for key in KEYS}
    moved = {key for key in KEYS if before[key] != after[key]}
    assert moved == {key for key in KEYS if before[key] == "B"}
    assert "B" not in after.values()
    clock.advance(30)
    assert {key: lb.pick(key).name for key in KEYS} == before
    assert {lb.pick(None).name for _ in range(3)} == {"A", "B", "C"}  # keyless falls back to RR


def test_passive_ejection_after_consecutive_errors_grows_and_expires() -> None:
    clock = FakeClock(start=100.0)
    lb = Balancer(pool(1, 1), RoundRobin(), clock, HealthPolicy(max_consecutive_errors=3, ejection_seconds=10.0))
    lb.report("A", ok=False)
    lb.report("A", ok=False)
    lb.report("A", ok=True)  # a success resets the streak
    lb.report("A", ok=False)
    lb.report("A", ok=False)
    assert lb.status("A") == "healthy"
    lb.report("A", ok=False)
    assert lb.status("A") == "ejected for 10s"
    assert lb.available() == ["B"]
    clock.advance(9.9)
    assert lb.available() == ["B"]
    clock.advance(0.1)
    assert lb.available() == ["A", "B"] and lb.status("A") == "healthy"
    for _ in range(3):
        lb.report("A", ok=False)
    assert lb.status("A") == "ejected for 20s"  # second ejection lasts twice as long


def test_active_probes_use_thresholds_in_both_directions() -> None:
    lb = Balancer(pool(1, 1), RoundRobin(), FakeClock(), HealthPolicy(unhealthy_threshold=2, healthy_threshold=3))
    lb.probe("A", ok=False)
    assert lb.status("A") == "healthy"
    lb.probe("A", ok=False)
    assert lb.status("A") == "unhealthy" and lb.available() == ["B"]
    lb.probe("A", ok=True)
    lb.probe("A", ok=True)
    assert lb.status("A") == "unhealthy"
    lb.probe("A", ok=True)
    assert lb.status("A") == "healthy" and lb.available() == ["A", "B"]


def test_lease_reports_exceptions_as_failures_and_releases_the_connection() -> None:
    lb = Balancer(pool(1), RoundRobin(), FakeClock(), HealthPolicy(max_consecutive_errors=2))
    for _ in range(2):
        with pytest.raises(ConnectionError), lb.lease() as backend:
            assert backend.active == 1
            raise ConnectionError("upstream reset")
    assert backend.active == 0
    with pytest.raises(NoAvailableBackend):
        lb.pick()


def test_validation_and_lookup_errors() -> None:
    lb = Balancer(pool(1, 1), RoundRobin(), FakeClock())
    with pytest.raises(ValidationError):
        lb.add(Backend("Z", weight=0))
    with pytest.raises(ConflictError):
        lb.add(Backend("A"))
    with pytest.raises(NotFoundError):
        lb.remove("Z")
    with pytest.raises(NotFoundError):
        lb.report("Z", ok=True)
    lb.remove("A")
    lb.remove("B")
    with pytest.raises(NoAvailableBackend):
        lb.pick()


def test_concurrent_leases_keep_counts_consistent() -> None:
    lb = Balancer(pool(1, 1, 1, 1), RoundRobin(), FakeClock())
    seen: list[str] = []

    def one_request(i: int) -> str:
        with lb.lease(f"req:{i}") as backend:
            return backend.name

    with ThreadPoolExecutor(max_workers=8) as executor:
        seen = list(executor.map(one_request, range(1_600)))
    assert len(seen) == 1_600
    assert all(seen.count(name) == 400 for name in "ABCD")  # round robin under one lock is exact
    assert all(lb.pick().active == 0 for _ in range(4))
    assert lb.available() == ["A", "B", "C", "D"]
