"""Concurrency primitives an LLD round actually asks you to write: a read-write
lock, a bounded buffer, a double-checked-locking singleton, an optimistic store
with compare-and-set, and deadlock-free transfers by lock ordering. Races are
*forced* with a ``Barrier``, so the demo is reproducible and no test sleeps.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import ClassVar

from common import ConflictError, NotFoundError, ValidationError

BARRIER_TIMEOUT_SECONDS, LOCK_TIMEOUT_SECONDS, MAX_CAS_ATTEMPTS = 2.0, 0.05, 100


# --8<-- [start:counters]
class UnsafeCounter:
    """``value += 1`` is load, add, store. The GIL serialises *bytecodes*, not
    statements, so a thread can run between the two and lose someone's write."""

    def __init__(self) -> None:
        self.value = 0

    def increment(self) -> None:
        self.value += 1


class SafeCounter:
    """The same field with one lock. ``with`` releases it even when the body raises."""

    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def increment(self) -> None:
        with self._lock:
            self._value += 1

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


def forced_lost_update() -> int:
    """Pin the interleaving instead of hoping for it: both threads read, meet at
    the barrier, then write. Two increments applied, counter ends at 1, every run."""
    counter = UnsafeCounter()
    gate = threading.Barrier(2)

    def read_then_write() -> None:
        seen = counter.value
        gate.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        counter.value = seen + 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(read_then_write) for _ in range(2)]:
            future.result()
    return counter.value


# --8<-- [end:counters]
# --8<-- [start:rwlock]
class ReadWriteLock:
    """Many concurrent readers, one exclusive writer, writers preferred.

    The condition's own lock guards ``_readers``, ``_writer`` and
    ``_waiting_writers``. A reader waits while a writer is active *or* queued,
    so readers cannot starve a writer; the mirror risk is the trade you accept.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    def acquire_read(self) -> None:
        with self._condition:
            while self._writer or self._waiting_writers:
                self._condition.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._condition:
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    def acquire_write(self) -> None:
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = True

    def release_write(self) -> None:
        with self._condition:
            self._writer = False
            self._condition.notify_all()

    @contextmanager
    def read_locked(self) -> Iterator[None]:
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write_locked(self) -> Iterator[None]:
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()


# --8<-- [end:rwlock]
# --8<-- [start:buffer]
class BoundedBuffer[T]:
    """Producer-consumer: two condition variables sharing one lock.

    Both waits sit inside a ``while`` because a wakeup is a hint, not a promise,
    and two conditions mean a ``put`` wakes a consumer, not every producer too.
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValidationError("capacity must be positive")
        self._capacity = capacity
        self._items: deque[T] = deque()
        self._lock = threading.Lock()
        self._not_full = threading.Condition(self._lock)
        self._not_empty = threading.Condition(self._lock)

    def put(self, item: T) -> None:
        with self._not_full:
            while len(self._items) == self._capacity:
                self._not_full.wait()
            self._items.append(item)
            self._not_empty.notify()

    def get(self) -> T:
        with self._not_empty:
            while not self._items:
                self._not_empty.wait()
            item = self._items.popleft()
            self._not_full.notify()
            return item

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# --8<-- [end:buffer]
# --8<-- [start:singleton]
class MetricsRegistry:
    """One instance per process, published under double-checked locking.

    The unlocked first check answers the common case; the second, under
    ``_instance_lock``, is the correct one. On CPython the store to
    ``_instance`` happens only after ``cls()`` returns, so no thread sees a
    half-built registry. ``_instance_lock`` guards creation, ``_lock`` the counters.
    """

    _instance: ClassVar[MetricsRegistry | None] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> MetricsRegistry:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._instance_lock:  # test hook; needing one hints that injection is simpler
            cls._instance = None

    def increment(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + by

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)


# --8<-- [end:singleton]
# --8<-- [start:optimistic]
@dataclass(frozen=True, slots=True)
class Versioned[T]:
    """A value and the version it was read at; immutable, so a stale copy stays stale."""

    value: T
    version: int


class OptimisticStore[T]:
    """Compare-and-set on a version: detect contention instead of preventing it.
    ``_lock`` is an ``RLock`` so the pessimistic path can reuse ``read``."""

    def __init__(self) -> None:
        self._entries: dict[str, Versioned[T]] = {}
        self._lock = threading.RLock()

    def put(self, key: str, value: T) -> Versioned[T]:
        with self._lock:
            entry = Versioned(value, 1)
            self._entries[key] = entry
            return entry

    def read(self, key: str) -> Versioned[T]:
        with self._lock:
            try:
                return self._entries[key]
            except KeyError:
                raise NotFoundError(f"no entry for {key!r}") from None

    def compare_and_set(self, key: str, expected_version: int, value: T) -> Versioned[T]:
        with self._lock:
            current = self.read(key)
            if current.version != expected_version:
                raise ConflictError(
                    f"{key!r} moved on: expected version {expected_version}, found {current.version}"
                )
            updated = Versioned(value, current.version + 1)
            self._entries[key] = updated
            return updated

    def update(self, key: str, change: Callable[[T], T], attempts: int = MAX_CAS_ATTEMPTS) -> Versioned[T]:
        """Read, compute outside the lock, write only if nothing moved underneath.
        Bounded, so a permanently hot key raises instead of spinning forever."""
        for _ in range(attempts):
            current = self.read(key)
            try:
                return self.compare_and_set(key, current.version, change(current.value))
            except ConflictError:
                continue
        raise ConflictError(f"gave up on {key!r} after {attempts} attempts")

    def update_pessimistically(self, key: str, change: Callable[[T], T]) -> Versioned[T]:
        """The pessimistic twin: hold the lock across the whole read-modify-write.
        No retries, no lost updates, but every writer waits for ``change``."""
        with self._lock:
            current = self.read(key)
            return self.compare_and_set(key, current.version, change(current.value))


# --8<-- [end:optimistic]
# --8<-- [start:ordering]
@dataclass(slots=True)
class Account:
    id: str
    balance: int
    lock: threading.Lock = field(default_factory=threading.Lock)


def transfer(source: Account, target: Account, amount: int) -> None:
    """Deadlock-free because both locks are taken in ``id`` order: ``transfer(a, b)``
    racing ``transfer(b, a)`` is the textbook cycle, and a stable key removes it."""
    if amount <= 0:
        raise ValidationError("transfer amount must be positive")
    if source.id == target.id:
        raise ValidationError("cannot transfer to the same account")
    first, second = sorted((source, target), key=lambda account: account.id)
    with first.lock, second.lock:
        if source.balance < amount:
            raise ConflictError(f"{source.id} holds {source.balance}, not {amount}")
        source.balance -= amount
        target.balance += amount


def try_transfer(source: Account, target: Account, amount: int, timeout: float = LOCK_TIMEOUT_SECONDS) -> bool:
    """The other cure when no global order exists: bounded acquisition, so the
    caller gets a back-off decision instead of a hang to debug."""
    if not source.lock.acquire(timeout=timeout):
        return False
    try:
        if not target.lock.acquire(timeout=timeout):
            return False
        try:
            if source.balance < amount:
                return False
            source.balance -= amount
            target.balance += amount
            return True
        finally:
            target.lock.release()
    finally:
        source.lock.release()


# --8<-- [end:ordering]


def _write_once(lock: ReadWriteLock, guarded: SafeCounter) -> None:
    with lock.write_locked():
        guarded.increment()


def _shuttle(left: Account, right: Account, worker: int, moves: int) -> None:
    """Half the workers move money left to right and half the other way: the deadlock setup."""
    source, target = (left, right) if worker % 2 == 0 else (right, left)
    for _ in range(moves):
        transfer(source, target, 1)


def _observe_concurrent_readers(lock: ReadWriteLock, readers: int) -> int:
    gate = threading.Barrier(readers)  # readers really overlap or this times out
    seen = SafeCounter()

    def read() -> None:
        with lock.read_locked():
            seen.increment()
            gate.wait(timeout=BARRIER_TIMEOUT_SECONDS)

    with ThreadPoolExecutor(max_workers=readers) as pool:
        for future in [pool.submit(read) for _ in range(readers)]:
            future.result()
    return seen.value


def _run_all(pool: ThreadPoolExecutor, work: Callable[[int], None], times: int) -> None:
    for future in [pool.submit(work, index) for index in range(times)]:
        future.result()


def main() -> None:
    print("--- += is not atomic: two threads read 0, both write 1 ---")
    print(f"2 increments applied, counter shows {forced_lost_update()}")

    counter = SafeCounter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        _run_all(pool, lambda _: [counter.increment() for _ in range(5_000)], 8)
    print(f"--- one Lock, 8 threads x 5000 increments: expected 40000, got {counter.value} ---")

    rw_lock, guarded = ReadWriteLock(), SafeCounter()
    concurrent_readers = _observe_concurrent_readers(rw_lock, readers=5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        _run_all(pool, lambda _: _write_once(rw_lock, guarded), 4)
    print("--- read-write lock ---")
    print(f"{concurrent_readers} readers inside at once; value after 4 exclusive writers: {guarded.value}")

    buffer: BoundedBuffer[int] = BoundedBuffer(capacity=2)
    with ThreadPoolExecutor(max_workers=5) as pool:
        work = [pool.submit(lambda i=i: [buffer.put(i * 4 + n) for n in range(4)]) for i in range(3)]
        work += [pool.submit(lambda: [buffer.get() for _ in range(6)]) for _ in range(2)]
        for future in work:
            future.result()
    print(f"--- bounded buffer (capacity 2): 12 items through, {len(buffer)} left ---")

    MetricsRegistry.reset()
    racers = 16
    start = threading.Barrier(racers)

    def register(_: int) -> MetricsRegistry:
        start.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        registry = MetricsRegistry.instance()
        registry.increment("requests")
        return registry

    with ThreadPoolExecutor(max_workers=racers) as pool:
        built = [f.result() for f in [pool.submit(register, i) for i in range(racers)]]
    print(f"--- singleton: {racers} threads race ---")
    print(f"distinct instances: {len({id(x) for x in built})}; counted: {built[0].snapshot()}")

    store: OptimisticStore[int] = OptimisticStore()
    store.put("views", 0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        _run_all(pool, lambda _: [store.update("views", lambda v: v + 1) for _ in range(25)], 4)
    final = store.read("views")
    print(f"--- optimistic CAS: 4 threads x 25 updates -> value {final.value} at version {final.version} ---")

    left, right = Account("A", 300), Account("B", 300)
    with ThreadPoolExecutor(max_workers=8) as pool:
        _run_all(pool, lambda worker: _shuttle(left, right, worker, 50), 8)
    print("--- lock ordering: 8 workers transferring both ways ---")
    print(f"balances {left.balance} and {right.balance}, total conserved at {left.balance + right.balance}")


if __name__ == "__main__":
    main()
