"""Object Pool: lend out expensive objects instead of building one per use.

The running example is a database connection pool. ``Connection`` is slow to
open and can go stale; ``ConnectionFactory`` does the expensive opening;
``ObjectPool`` keeps a bounded set of idle objects in a ``queue.Queue``, hands
one out per ``acquire`` and takes it back on ``release``; ``ConnectionPool``
wires the generic pool to the connection lifecycle (health check, reset,
close). ``lease()`` is the context manager callers should use, so a forgotten
``release`` is impossible. The second half shows the ten-line Pythonic pool
that is enough when objects never go bad.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

from common import (
    HandbookError,
    IdGenerator,
    InvalidStateError,
    SequentialIdGenerator,
    ValidationError,
)

DEFAULT_TIMEOUT_SECONDS = 30.0
RECHECK_SECONDS = 0.05


class PoolExhaustedError(HandbookError):
    """Every object is lent out and nobody released one within the timeout."""


# --8<-- [start:connection]
@dataclass(slots=True)
class Connection:
    """Stands in for a real connection: slow to open, cheap to reuse, able to go stale."""

    conn_id: str
    healthy: bool = True
    queries: int = 0
    in_transaction: bool = False
    closed: bool = False

    def query(self, sql: str) -> str:
        if self.closed or not self.healthy:
            raise InvalidStateError(f"{self.conn_id} is not usable")
        self.queries += 1
        self.in_transaction = True
        return f"{self.conn_id} ran {sql!r}"

    def is_healthy(self) -> bool:
        return self.healthy and not self.closed

    def reset(self) -> None:
        """Rollback on release, so the next borrower starts from a clean state."""
        self.in_transaction = False

    def close(self) -> None:
        self.closed = True


class ConnectionFactory:
    """Opening a connection is the expensive step the pool exists to amortise.

    ``_lock`` protects ``_opened``, the count that lets tests and the demo prove reuse.
    """

    def __init__(self, ids: IdGenerator | None = None) -> None:
        self._ids = ids or SequentialIdGenerator("conn")
        self._opened = 0
        self._lock = threading.Lock()

    @property
    def opened(self) -> int:
        with self._lock:
            return self._opened

    def __call__(self) -> Connection:
        with self._lock:
            self._opened += 1
        return Connection(self._ids.next_id())


# --8<-- [end:connection]


# --8<-- [start:pool]
class ObjectPool[T]:
    """A bounded, thread-safe pool of interchangeable objects.

    Locking: the ``queue.Queue`` of idle objects is its own lock and supplies the
    blocking wait; ``_lock`` protects ``_created``, ``_lent`` and ``_closed``.
    Objects are created lazily, so a pool with ``max_size`` 8 that only ever
    needs two opens two. The three hooks are optional: ``validate`` runs on
    acquire (a stale object is destroyed and replaced), ``reset`` on release,
    ``destroy`` whenever the pool lets go of an object. Every wait is bounded:
    a pool that can block forever turns one leaked object into a hung service.
    """

    def __init__(
        self,
        factory: Callable[[], T],
        max_size: int,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        validate: Callable[[T], bool] | None = None,
        reset: Callable[[T], None] | None = None,
        destroy: Callable[[T], None] | None = None,
    ) -> None:
        if max_size < 1:
            raise ValidationError("max_size must be at least 1")
        if timeout <= 0:
            raise ValidationError("timeout must be positive")
        self._factory = factory
        self._max_size = max_size
        self._timeout = timeout
        self._validate = validate
        self._reset = reset
        self._destroy = destroy
        self._idle: queue.Queue[T] = queue.Queue(maxsize=max_size)
        self._lock = threading.Lock()
        self._created = 0
        self._lent: dict[int, T] = {}  # id -> object; holding it keeps the id unique
        self._closed = False

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def size(self) -> int:
        """Objects alive, idle or lent."""
        with self._lock:
            return self._created

    @property
    def in_use(self) -> int:
        with self._lock:
            return len(self._lent)

    @property
    def available(self) -> int:
        return self._idle.qsize()

    def acquire(self, timeout: float | None = None) -> T:
        """Take an idle object, create one if the cap allows, else wait (bounded) for a release."""
        with self._lock:
            if self._closed:
                raise InvalidStateError("pool is closed")
        obj = self._take_idle()
        if obj is None:
            obj = self._create_if_room()
        if obj is None:
            obj = self._wait_for_object(self._timeout if timeout is None else timeout)
        if self._validate is not None and not self._validate(obj):
            obj = self._replace(obj)
        with self._lock:
            self._lent[id(obj)] = obj
        return obj

    def release(self, obj: T) -> None:
        """Give an object back; refuses objects this pool did not lend (double release)."""
        with self._lock:
            if self._lent.pop(id(obj), None) is None:
                raise ValidationError("object was not acquired from this pool, or was already released")
        if self._reset is not None:
            try:
                self._reset(obj)
            except Exception:
                self._dispose(obj)
                raise
        with self._lock:
            closed = self._closed
            if not closed:
                self._idle.put_nowait(obj)  # never full: at most max_size objects exist
        if closed:
            self._dispose(obj)

    def discard(self, obj: T) -> None:
        """For a borrower who knows the object is broken: drop it and free its slot."""
        with self._lock:
            if self._lent.pop(id(obj), None) is None:
                raise ValidationError("object was not acquired from this pool")
        self._dispose(obj)

    @contextmanager
    def lease(self, timeout: float | None = None) -> Iterator[T]:
        """``with pool.lease() as conn:`` releases on every exit path, exceptions included."""
        obj = self.acquire(timeout)
        try:
            yield obj
        finally:
            self.release(obj)

    def close(self) -> None:
        """Destroy idle objects now and lent ones as they come back; new borrowers are refused."""
        with self._lock:
            self._closed = True
            idle: list[T] = []
            while True:
                try:
                    idle.append(self._idle.get_nowait())
                except queue.Empty:
                    break
        for obj in idle:
            self._dispose(obj)

# --8<-- [end:pool]

# --8<-- [start:internals]
    def _take_idle(self) -> T | None:
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            return None

    def _wait_for_object(self, wait: float) -> T:
        """Wait up to ``wait`` seconds for an object, then give up with a typed error.

        A ``release`` wakes this immediately through the queue's condition variable.
        The deadline is sliced because ``discard`` and a failed ``reset`` free a *slot*
        without queueing an object: without the re-check a borrower already waiting
        would block for the whole timeout while the pool sat empty.
        """
        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PoolExhaustedError(
                    f"all {self._max_size} objects are in use (waited {wait} s)"
                )
            try:
                return self._idle.get(timeout=min(remaining, RECHECK_SECONDS))
            except queue.Empty:
                obj = self._create_if_room()
                if obj is not None:
                    return obj

    def _create_if_room(self) -> T | None:
        """Reserve a slot under the lock, then run the slow factory outside it."""
        with self._lock:
            if self._closed or self._created >= self._max_size:
                return None
            self._created += 1
        try:
            return self._factory()
        except Exception:
            with self._lock:
                self._created -= 1  # the slot goes back, so the next borrower may try again
            raise

    def _replace(self, stale: T) -> T:
        """Destroy a stale object and build a fresh one in the same slot."""
        if self._destroy is not None:
            self._destroy(stale)
        try:
            return self._factory()
        except Exception:
            with self._lock:
                self._created -= 1
            raise

    def _dispose(self, obj: T) -> None:
        if self._destroy is not None:
            self._destroy(obj)
        with self._lock:
            self._created -= 1


class ConnectionPool(ObjectPool[Connection]):
    """The pool interviews ask for: the generic pool wired to Connection's lifecycle."""

    def __init__(
        self,
        factory: ConnectionFactory,
        max_size: int = 4,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            factory,
            max_size,
            timeout=timeout,
            validate=Connection.is_healthy,
            reset=Connection.reset,
            destroy=Connection.close,
        )


# --8<-- [end:internals]


# --8<-- [start:pythonic]
def prefilled_pool(factory: Callable[[], Connection], size: int) -> queue.Queue[Connection]:
    """Eager and fixed-size: the queue is the pool, and a blocking ``get`` is the wait."""
    idle: queue.Queue[Connection] = queue.Queue(maxsize=size)
    for _ in range(size):
        idle.put(factory())
    return idle


@contextmanager
def borrow(idle: queue.Queue[Connection], timeout: float | None = None) -> Iterator[Connection]:
    conn = idle.get(timeout=timeout)  # blocks until a peer puts one back
    try:
        yield conn
    finally:
        idle.put(conn)


# --8<-- [end:pythonic]


def main() -> None:
    factory = ConnectionFactory()
    pool = ConnectionPool(factory, max_size=3)
    print("--- 6 sequential borrowers, pool of 3 ---")
    for n in range(1, 7):
        with pool.lease() as conn:
            print(f"  borrower {n}: {conn.query(f'SELECT {n}')}")
    print(f"  connections opened: {factory.opened} (one is enough when nobody overlaps)")

    print("--- 3 borrowers inside the pool at once, then 8 threads x 25 leases ---")
    together = threading.Barrier(3)

    def hold(_: int) -> str:
        with pool.lease() as conn:
            together.wait()  # nobody releases until all three hold a connection
            return conn.conn_id

    with ThreadPoolExecutor(max_workers=3) as executor:
        held = sorted(executor.map(hold, range(3)))
    print(f"  held at the same time: {held}; opened: {factory.opened}")

    def hammer(_: int) -> int:
        with pool.lease() as conn:
            conn.query("SELECT now()")
        return 1

    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = sum(executor.map(hammer, range(200)))
    print(f"  leases: {leases}, opened: {factory.opened} (the cap held), in use afterwards: {pool.in_use}")

    print("--- a stale idle connection is replaced on acquire (pool of 1) ---")
    small_factory = ConnectionFactory(SequentialIdGenerator("db"))
    small = ConnectionPool(small_factory, max_size=1)
    conn = small.acquire()
    conn.healthy = False  # the server dropped it while we held it
    small.release(conn)
    fresh = small.acquire()
    print(f"  {conn.conn_id} failed the health check; got {fresh.conn_id}, closed old: {conn.closed}")
    print(f"  pool size still {small.size}, opened {small_factory.opened}")

    print("--- bounded wait: both connections lent out, a third borrower gives up after 50 ms ---")
    pair = ConnectionPool(ConnectionFactory(SequentialIdGenerator("c")), max_size=2)
    first, second = pair.acquire(), pair.acquire()
    try:
        pair.acquire(timeout=0.05)
    except PoolExhaustedError as exc:
        print(f"  PoolExhaustedError: {exc}")
    pair.release(first)
    print(f"  after one release: got {pair.acquire().conn_id} immediately")

    print("--- a double release is refused ---")
    pair.release(second)
    try:
        pair.release(second)
    except ValidationError as exc:
        print(f"  ValidationError: {exc}")

    print("--- close(): idle connections are closed, new borrowers are refused ---")
    pool.close()
    try:
        pool.acquire()
    except InvalidStateError as exc:
        print(f"  InvalidStateError: {exc}; size now {pool.size}")


if __name__ == "__main__":
    main()
