"""The two concurrency primitives: the timing heap and the worker pool.

Both are deliberately small and independent of the scheduler, so each can be
tested - and reasoned about - on its own.
"""

from __future__ import annotations

import heapq
import queue
import threading
from collections.abc import Callable

from common import Clock, ValidationError
from lld.task_scheduler.models import QueueEntry, SchedulerStateError

DEFAULT_WORKERS = 4
DEFAULT_SHUTDOWN_TIMEOUT = 2.0


# --8<-- [start:queue]
class TaskQueue:
    """A min-heap of due times behind one ``Condition``: the timer half of the scheduler.

    ``pop_due`` is the method the whole design turns on. It waits for exactly as
    long as the earliest entry is not yet due - never a poll loop - and ``push``
    notifies, so scheduling something *sooner* than the current head wakes the
    timer immediately instead of leaving it parked for the old delay.

    ``_condition`` guards ``_heap`` and ``_closed``. Nothing else touches them.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._condition = threading.Condition()
        self._heap: list[QueueEntry] = []
        self._closed = False

    def push(self, entry: QueueEntry) -> None:
        with self._condition:
            heapq.heappush(self._heap, entry)
            self._condition.notify_all()  # an earlier due time must re-arm the timer

    def pop_due(self) -> QueueEntry | None:
        """Block until the earliest entry is due. ``None`` means the queue is closed."""
        with self._condition:
            while not self._closed:
                if not self._heap:
                    self._condition.wait()  # nothing to time: sleep until a push
                    continue
                delay = self._heap[0].due_at - self._clock.now()
                if delay <= 0:
                    return heapq.heappop(self._heap)
                self._condition.wait(timeout=delay)
            return None

    def wake(self) -> None:
        """Force a re-evaluation. Production never needs it; a test with a fake clock does."""
        with self._condition:
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def pending(self) -> list[QueueEntry]:
        with self._condition:
            return sorted(self._heap)

    def __len__(self) -> int:
        with self._condition:
            return len(self._heap)


# --8<-- [end:queue]


# --8<-- [start:pool]
class WorkerPool:
    """Fixed threads draining a bounded queue - the consumer half of producer-consumer.

    The bound is deliberate. If work arrives faster than it finishes, ``submit``
    blocks the timer thread, and that backpressure is the honest behaviour: an
    unbounded queue would absorb the backlog silently until memory ran out.
    Shutdown pushes one sentinel per worker, so every thread leaves its blocking
    ``get`` without a flag anyone has to poll.
    """

    def __init__(self, size: int = DEFAULT_WORKERS, name: str = "worker", queue_size: int = 0) -> None:
        if size < 1:
            raise ValidationError("worker pool needs at least one thread")
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue(maxsize=queue_size or size * 4)
        self._threads = [
            threading.Thread(target=self._run, name=f"{name}-{i}", daemon=True) for i in range(size)
        ]
        self._started = False
        self._closed = False

    @property
    def size(self) -> int:
        return len(self._threads)

    def start(self) -> None:
        self._started = True
        for thread in self._threads:
            thread.start()

    def submit(self, job: Callable[[], None]) -> None:
        if self._closed:
            raise SchedulerStateError("worker pool is shut down")
        self._queue.put(job)

    def shutdown(self, drain: bool = True, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """``drain=True`` finishes what is queued. ``drain=False`` discards it first."""
        self._closed = True
        if not drain:
            self._discard()
        for _ in self._threads:
            self._queue.put(None)
        if self._started:
            for thread in self._threads:
                thread.join(timeout)

    def _discard(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
            self._queue.task_done()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                job()
            finally:
                self._queue.task_done()


# --8<-- [end:pool]
