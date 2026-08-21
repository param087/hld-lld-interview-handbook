"""Object Pool: lazy creation, reuse, bounded waits, health checks and safe concurrent leasing."""

import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import InvalidStateError, SequentialIdGenerator, ValidationError
from patterns.object_pool import (
    Connection,
    ConnectionFactory,
    ConnectionPool,
    ObjectPool,
    PoolExhaustedError,
    borrow,
    prefilled_pool,
)


def make_pool(max_size: int = 3) -> tuple[ConnectionFactory, ConnectionPool]:
    factory = ConnectionFactory(SequentialIdGenerator("conn"))
    return factory, ConnectionPool(factory, max_size=max_size)


@pytest.mark.parametrize(("max_size", "timeout"), [(0, 1.0), (-1, 1.0), (1, 0.0), (1, -2.0)])
def test_pool_rejects_an_empty_cap_or_an_unbounded_wait(max_size: int, timeout: float) -> None:
    with pytest.raises(ValidationError):
        ObjectPool(ConnectionFactory(), max_size, timeout=timeout)


def test_objects_are_created_lazily_and_reused_after_release() -> None:
    factory, pool = make_pool(max_size=3)
    assert (pool.size, pool.available, pool.in_use) == (0, 0, 0)
    first = pool.acquire()
    pool.release(first)
    second = pool.acquire()
    assert second is first  # the idle one comes back before a new one is opened
    assert factory.opened == 1
    pool.release(second)
    a, b = pool.acquire(), pool.acquire()
    assert a is not b and factory.opened == 2
    assert (pool.size, pool.available, pool.in_use) == (2, 0, 2)


def test_lease_releases_on_exceptions_and_resets_the_object() -> None:
    _, pool = make_pool(max_size=1)
    with pytest.raises(RuntimeError):
        with pool.lease() as conn:
            conn.query("BEGIN")
            assert conn.in_transaction
            raise RuntimeError("caller blew up mid-transaction")
    assert pool.in_use == 0 and pool.available == 1
    with pool.lease() as again:
        assert again is conn and not again.in_transaction  # reset ran on release


def test_exhausted_pool_waits_then_raises_and_recovers_after_a_release() -> None:
    _, pool = make_pool(max_size=2)
    first, _second = pool.acquire(), pool.acquire()
    with pytest.raises(PoolExhaustedError):
        pool.acquire(timeout=0.01)
    pool.release(first)
    assert pool.acquire(timeout=0.01) is first


def test_a_waiting_borrower_is_woken_by_a_release_from_another_thread() -> None:
    _, pool = make_pool(max_size=1)
    held = pool.acquire()
    got: list[Connection] = []
    started = threading.Event()

    def waiter() -> None:
        started.set()
        got.append(pool.acquire(timeout=2.0))

    thread = threading.Thread(target=waiter)
    thread.start()
    started.wait(timeout=2.0)
    pool.release(held)
    thread.join(timeout=2.0)
    assert got == [held] and pool.in_use == 1


def test_stale_idle_object_is_destroyed_and_replaced_within_the_cap() -> None:
    factory, pool = make_pool(max_size=1)
    stale = pool.acquire()
    stale.healthy = False
    pool.release(stale)
    fresh = pool.acquire()
    assert fresh is not stale and fresh.is_healthy()
    assert stale.closed  # destroy hook ran on the stale one
    assert factory.opened == 2 and pool.size == 1  # same slot, new object


def test_release_and_discard_reject_objects_the_pool_did_not_lend() -> None:
    _, pool = make_pool(max_size=2)
    conn = pool.acquire()
    pool.release(conn)
    with pytest.raises(ValidationError):
        pool.release(conn)  # double release would let two borrowers share it
    with pytest.raises(ValidationError):
        pool.release(Connection("foreign"))
    broken = pool.acquire()
    pool.discard(broken)
    assert broken.closed and pool.size == 0  # the slot is free again
    with pytest.raises(ValidationError):
        pool.discard(broken)


def test_close_destroys_idle_objects_refuses_new_borrowers_and_drains_late_returns() -> None:
    _, pool = make_pool(max_size=2)
    idle, lent = pool.acquire(), pool.acquire()
    pool.release(idle)
    pool.close()
    assert idle.closed and pool.size == 1 and pool.available == 0
    with pytest.raises(InvalidStateError):
        pool.acquire()
    pool.release(lent)
    assert lent.closed and pool.size == 0


def test_factory_failure_frees_the_reserved_slot() -> None:
    calls = {"n": 0}

    def flaky() -> Connection:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("database down")
        return Connection(f"conn-{calls['n']}")

    pool: ObjectPool[Connection] = ObjectPool(flaky, max_size=1)
    with pytest.raises(ConnectionError):
        pool.acquire()
    assert pool.size == 0
    assert pool.acquire().conn_id == "conn-2"


def test_concurrent_borrowers_never_share_an_object_and_never_exceed_the_cap() -> None:
    factory, pool = make_pool(max_size=3)
    busy: set[str] = set()
    busy_lock = threading.Lock()
    overlaps: list[str] = []

    def work(_: int) -> None:
        with pool.lease(timeout=2.0) as conn:
            with busy_lock:
                if conn.conn_id in busy:
                    overlaps.append(conn.conn_id)
                busy.add(conn.conn_id)
            conn.query("SELECT 1")
            with busy_lock:
                busy.discard(conn.conn_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(work, range(400)))
    assert overlaps == []
    assert factory.opened <= 3 and pool.size <= 3
    assert pool.in_use == 0 and pool.available == pool.size


def test_pythonic_queue_pool_blocks_and_reuses_like_the_class() -> None:
    factory = ConnectionFactory(SequentialIdGenerator("q"))
    idle = prefilled_pool(factory, size=2)
    assert factory.opened == 2 and idle.qsize() == 2
    with borrow(idle) as first, borrow(idle) as second:
        assert first is not second and idle.empty()
        with pytest.raises(queue.Empty):  # queue.Empty is the whole error handling
            with borrow(idle, timeout=0.01):
                pass
    assert idle.qsize() == 2
