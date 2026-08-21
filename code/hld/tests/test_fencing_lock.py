import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.fencing_lock import FencedStore, FencingError, Lease, LeaseLockService


def make(ttl: float = 10.0) -> tuple[FakeClock, LeaseLockService]:
    clock = FakeClock(start=1_000.0)
    return clock, LeaseLockService(clock, ttl=ttl)


def test_lease_is_exclusive_until_it_expires_and_tokens_increase() -> None:
    clock, service = make()
    first = service.acquire("job:1", "A")
    assert first == Lease("job:1", "A", 1, 1_010.0)
    assert service.acquire("job:1", "B") is None
    assert service.acquire("job:1", "A") is None  # no re-entrant grant: renew instead
    clock.advance(9.999)
    assert service.acquire("job:1", "B") is None
    clock.advance(0.001)
    assert service.holder("job:1") is None
    second = service.acquire("job:1", "B")
    assert second is not None and second.token == 2 and second.owner == "B"
    assert service.holder("job:1") == second


def test_renew_extends_only_a_current_lease() -> None:
    clock, service = make()
    lease = service.acquire("job:1", "A")
    assert lease is not None
    clock.advance(8)
    renewed = service.renew(lease)
    assert renewed is not None
    assert renewed.token == lease.token and renewed.expires_at == clock.now() + 10
    assert service.renew(lease) is not None  # an older Lease object with the same token is fine
    clock.advance(11)
    assert service.renew(renewed) is None  # expired: the keep-alive came too late
    taken = service.acquire("job:1", "B")
    assert taken is not None
    assert service.renew(renewed) is None  # and now somebody else holds it


def test_release_ignores_stale_leases() -> None:
    clock, service = make()
    lease = service.acquire("job:1", "A")
    assert lease is not None
    clock.advance(11)
    successor = service.acquire("job:1", "B")
    assert successor is not None
    assert service.release(lease) is False  # A's lease is history; B keeps the lock
    assert service.holder("job:1") == successor
    assert service.release(successor) is True
    assert service.release(successor) is False
    fresh = service.acquire("job:1", "A")
    assert fresh is not None and fresh.token == 3  # tokens are never reused


def test_tokens_are_strictly_increasing_across_resources() -> None:
    _, service = make()
    tokens = []
    for i in range(20):
        lease = service.acquire(f"res:{i % 4}", "A")
        if lease is None:
            continue
        tokens.append(lease.token)
        service.release(lease)
    assert tokens == sorted(tokens) and len(set(tokens)) == len(tokens)


def test_paused_holder_cannot_overwrite_a_newer_holder_when_the_store_checks_tokens() -> None:
    clock, service = make()
    store = FencedStore()
    lease_a = service.acquire("job:1", "A")
    assert lease_a is not None
    store.write("result", "A-first", lease_a.token)
    clock.advance(12)  # A is paused past its ttl
    lease_b = service.acquire("job:1", "B")
    assert lease_b is not None and lease_b.token > lease_a.token
    store.write("result", "B-first", lease_b.token)
    with pytest.raises(FencingError):
        store.write("result", "A-second", lease_a.token)
    assert store.read("result") == "B-first" and store.token_of("result") == lease_b.token
    assert store.rejected == 1
    store.write("result", "B-second", lease_b.token)  # equal token: the holder writes again
    assert store.read("result") == "B-second"
    assert lease_a.remaining(clock.now()) < 0


def test_without_token_checks_the_stale_write_wins_and_newer_work_is_lost() -> None:
    clock, service = make()
    store = FencedStore(check_tokens=False)
    lease_a = service.acquire("job:1", "A")
    assert lease_a is not None
    clock.advance(12)
    lease_b = service.acquire("job:1", "B")
    assert lease_b is not None
    store.write("result", "B-first", lease_b.token)
    store.write("result", "A-second", lease_a.token)  # accepted: the classic lost update
    assert store.read("result") == "A-second" and store.rejected == 0


def test_validation_errors() -> None:
    clock = FakeClock()
    with pytest.raises(ValidationError):
        LeaseLockService(clock, ttl=0)
    service = LeaseLockService(clock)
    with pytest.raises(ValidationError):
        service.acquire("", "A")
    with pytest.raises(ValidationError):
        service.acquire("job:1", "")
    assert service.holder("missing") is None
    assert FencedStore().read("missing") is None


def test_concurrent_acquires_grant_one_lease_and_fenced_writes_never_regress() -> None:
    _, service = make()
    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(lambda i: service.acquire("job:1", f"worker-{i}"), range(64)))
    granted = [lease for lease in leases if lease is not None]
    assert len(granted) == 1
    assert service.holder("job:1") == granted[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        issued = list(pool.map(lambda i: service.acquire(f"res:{i}", "A"), range(200)))
    tokens = sorted(lease.token for lease in issued if lease is not None)
    assert len(tokens) == 200 and len(set(tokens)) == 200

    store = FencedStore()
    shuffled = list(tokens)
    random.Random(42).shuffle(shuffled)

    def write(token: int) -> bool:
        try:
            store.write("k", f"v{token}", token)
            return True
        except FencingError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(write, shuffled))
    assert store.token_of("k") == max(tokens) and store.read("k") == f"v{max(tokens)}"
    assert store.rejected == outcomes.count(False) == len(tokens) - outcomes.count(True)
    assert outcomes.count(True) >= 1
