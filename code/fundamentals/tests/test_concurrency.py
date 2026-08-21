"""Concurrency artifacts, tested deterministically: barriers force the races, nothing sleeps."""

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, NotFoundError, ValidationError
from fundamentals.concurrency import (
    Account,
    BoundedBuffer,
    MetricsRegistry,
    OptimisticStore,
    ReadWriteLock,
    SafeCounter,
    forced_lost_update,
    transfer,
    try_transfer,
)

BLOCKED_CHECK_SECONDS = 0.05  # long enough to prove a thread is blocked, short enough to stay fast
JOIN_TIMEOUT_SECONDS = 5.0
BARRIER_TIMEOUT_SECONDS = 2.0


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    """Process-wide state is why singletons are hard to test; this is the price."""
    MetricsRegistry.reset()
    yield
    MetricsRegistry.reset()


def run_all(work: Callable[[int], object], times: int, workers: int) -> None:
    """Run ``work`` on ``times`` threads and re-raise anything a worker swallowed."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(work, index) for index in range(times)]:
            future.result(timeout=JOIN_TIMEOUT_SECONDS)


def test_plus_equals_loses_an_update_when_the_interleaving_is_pinned() -> None:
    assert forced_lost_update() == 1  # two increments applied, one survives


def test_one_lock_makes_the_same_counter_exact() -> None:
    counter = SafeCounter()
    run_all(lambda _: [counter.increment() for _ in range(2_000)], times=8, workers=8)
    assert counter.value == 16_000


def test_readers_hold_the_read_lock_at_the_same_time() -> None:
    lock = ReadWriteLock()
    readers = 5
    gate = threading.Barrier(readers)

    def read(_: int) -> None:
        with lock.read_locked():
            gate.wait(timeout=BARRIER_TIMEOUT_SECONDS)  # times out unless all five are inside

    run_all(read, times=readers, workers=readers)


def test_a_writer_excludes_readers_until_it_releases() -> None:
    lock = ReadWriteLock()
    lock.acquire_write()
    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(_read_and_report, lock)
        with pytest.raises(TimeoutError):
            reader.result(timeout=BLOCKED_CHECK_SECONDS)
        lock.release_write()
        assert reader.result(timeout=JOIN_TIMEOUT_SECONDS) == "read"


def test_the_write_lock_serialises_a_read_modify_write() -> None:
    lock = ReadWriteLock()
    box = [0]

    def bump(_: int) -> None:
        for _ in range(200):
            with lock.write_locked():
                seen = box[0]
                box[0] = seen + 1  # deliberately not atomic; the lock is what saves it

    run_all(bump, times=8, workers=8)
    assert box[0] == 1_600


def test_bounded_buffer_delivers_every_item_exactly_once() -> None:
    buffer: BoundedBuffer[int] = BoundedBuffer(capacity=2)
    consumed: list[int] = []
    guard = threading.Lock()

    def produce(worker: int) -> None:
        for offset in range(4):
            buffer.put(worker * 4 + offset)

    def consume(_: int) -> None:
        for _ in range(6):
            item = buffer.get()
            with guard:
                consumed.append(item)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(produce, worker) for worker in range(3)]
        futures += [pool.submit(consume, worker) for worker in range(2)]
        for future in futures:
            future.result(timeout=JOIN_TIMEOUT_SECONDS)

    assert sorted(consumed) == list(range(12))
    assert len(buffer) == 0


def test_a_full_buffer_blocks_the_producer_until_a_consumer_takes_one() -> None:
    buffer: BoundedBuffer[int] = BoundedBuffer(capacity=2)
    buffer.put(1)
    buffer.put(2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(buffer.put, 3)
        with pytest.raises(TimeoutError):
            blocked.result(timeout=BLOCKED_CHECK_SECONDS)
        assert buffer.get() == 1
        blocked.result(timeout=JOIN_TIMEOUT_SECONDS)
    assert [buffer.get(), buffer.get()] == [2, 3]


@pytest.mark.parametrize("capacity", [0, -1])
def test_a_buffer_needs_a_positive_capacity(capacity: int) -> None:
    with pytest.raises(ValidationError):
        BoundedBuffer(capacity=capacity)


def test_double_checked_locking_builds_exactly_one_instance_under_a_race() -> None:
    racers = 16
    start = threading.Barrier(racers)

    def build(_: int) -> MetricsRegistry:
        start.wait(timeout=BARRIER_TIMEOUT_SECONDS)  # every thread arrives at instance() together
        registry = MetricsRegistry.instance()
        registry.increment("requests")
        return registry

    with ThreadPoolExecutor(max_workers=racers) as pool:
        built = [f.result(timeout=JOIN_TIMEOUT_SECONDS) for f in [pool.submit(build, i) for i in range(racers)]]

    assert len({id(instance) for instance in built}) == 1
    assert built[0].snapshot() == {"requests": racers}


def test_compare_and_set_rejects_a_stale_version_and_an_unknown_key() -> None:
    store: OptimisticStore[int] = OptimisticStore()
    store.put("views", 10)
    stale = store.read("views")
    store.compare_and_set("views", stale.version, 11)  # someone else wins the race

    with pytest.raises(ConflictError):
        store.compare_and_set("views", stale.version, 99)
    with pytest.raises(NotFoundError):
        store.read("missing")
    assert store.read("views").value == 11


@pytest.mark.parametrize("pessimistic", [False, True])
def test_both_locking_styles_apply_every_increment(pessimistic: bool) -> None:
    store: OptimisticStore[int] = OptimisticStore()
    store.put("views", 0)
    apply = store.update_pessimistically if pessimistic else store.update

    run_all(lambda _: [apply("views", lambda value: value + 1) for _ in range(25)], times=4, workers=4)

    final = store.read("views")
    assert (final.value, final.version) == (100, 101)


def test_optimistic_update_gives_up_instead_of_spinning() -> None:
    store: OptimisticStore[int] = OptimisticStore()
    store.put("views", 0)

    def change(value: int) -> int:
        store.compare_and_set("views", store.read("views").version, value)  # always steal the version
        return value + 1

    with pytest.raises(ConflictError, match="gave up"):
        store.update("views", change, attempts=3)


def test_transfers_in_both_directions_neither_deadlock_nor_lose_money() -> None:
    left, right = Account("A", 300), Account("B", 300)

    def shuttle(worker: int) -> None:
        source, target = (left, right) if worker % 2 == 0 else (right, left)
        for _ in range(50):
            transfer(source, target, 1)

    run_all(shuttle, times=8, workers=8)

    assert (left.balance, right.balance) == (300, 300)
    assert left.balance + right.balance == 600


def test_try_transfer_backs_off_rather_than_waiting_for_a_held_lock() -> None:
    left, right = Account("A", 100), Account("B", 100)
    right.lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            attempt = pool.submit(try_transfer, left, right, 10, BLOCKED_CHECK_SECONDS)
            assert attempt.result(timeout=JOIN_TIMEOUT_SECONDS) is False
    finally:
        right.lock.release()
    assert (left.balance, right.balance) == (100, 100)


@pytest.mark.parametrize(("amount", "same"), [(0, False), (-5, False), (10, True)])
def test_transfer_validates_before_it_takes_any_lock(amount: int, same: bool) -> None:
    left, right = Account("A", 100), Account("B", 100)
    target = left if same else right
    with pytest.raises(ValidationError):
        transfer(left, target, amount)
    assert not left.lock.locked() and not right.lock.locked()


def test_an_overdraft_is_refused_and_leaves_both_balances_alone() -> None:
    left, right = Account("A", 5), Account("B", 100)
    with pytest.raises(ConflictError):
        transfer(left, right, 10)
    assert (left.balance, right.balance) == (5, 100)


def _read_and_report(lock: ReadWriteLock) -> str:
    with lock.read_locked():
        return "read"
