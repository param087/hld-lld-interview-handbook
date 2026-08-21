"""Destinations. The other half of the Bridge: *where* a record goes.

Every handler owns its own lock. That is the design decision worth defending:
one global logging lock serialises unrelated destinations, one lock per handler
serialises only the writers that share a file descriptor.
"""

from __future__ import annotations

import queue
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TextIO

from lld.logging_framework.formatters import Formatter, PlainFormatter
from lld.logging_framework.models import (
    FileSystem,
    Filter,
    LogLevel,
    LogRecord,
    OverflowPolicy,
    QueueOverflowError,
    Transport,
    WorkerState,
)
from lld.logging_framework.sinks import LocalFileSystem


# --8<-- [start:handler_base]
class Handler(ABC):
    """Template Method: ``handle`` is fixed policy, ``emit`` is the varying step.

    A handler has its own threshold and its own filters, independent of the
    logger that reached it: one logger can feed a DEBUG file and a WARNING pager.
    """

    def __init__(
        self,
        formatter: Formatter | None = None,
        level: LogLevel = LogLevel.NOTSET,
        name: str = "",
    ) -> None:
        self.name = name or type(self).__name__
        self.level = level
        self.formatter: Formatter = formatter or PlainFormatter()
        self._filters: list[Filter] = []

    def add_filter(self, log_filter: Filter) -> Handler:
        self._filters.append(log_filter)
        return self

    def accepts(self, record: LogRecord) -> bool:
        return record.level >= self.level and all(f.allows(record) for f in self._filters)

    def handle(self, record: LogRecord) -> bool:
        """Returns True when the record was actually written by this handler."""
        if not self.accepts(record):
            return False
        self.emit(record)
        return True

    @abstractmethod
    def emit(self, record: LogRecord) -> None: ...

    def flush(self) -> None:
        """Push buffered records to the destination. Safe to call repeatedly."""
        return None  # unbuffered handlers have nothing to do

    def close(self) -> None:
        self.flush()


class NullHandler(Handler):
    """Null Object: the default a library attaches so it never warns about missing handlers."""

    def emit(self, record: LogRecord) -> None:
        return None


# --8<-- [end:handler_base]


# --8<-- [start:concrete_handlers]
class StreamHandler(Handler):
    """Console output. The lock keeps two threads from interleaving one line."""

    def __init__(self, stream: TextIO, formatter: Formatter | None = None, level: LogLevel = LogLevel.NOTSET) -> None:
        super().__init__(formatter, level, name="console")
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, record: LogRecord) -> None:
        line = self.formatter.format(record)
        with self._lock:
            self._stream.write(line + "\n")

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()


class InMemoryHandler(Handler):
    """Keeps the last ``capacity`` formatted lines. The assertion target in tests."""

    def __init__(self, formatter: Formatter | None = None, level: LogLevel = LogLevel.NOTSET, capacity: int = 1000) -> None:
        super().__init__(formatter, level, name="memory")
        self.capacity = capacity
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._records: list[LogRecord] = []

    def emit(self, record: LogRecord) -> None:
        line = self.formatter.format(record)
        with self._lock:
            self._lines.append(line)
            self._records.append(record)
            if len(self._lines) > self.capacity:
                del self._lines[: len(self._lines) - self.capacity]
                del self._records[: len(self._records) - self.capacity]

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def records(self) -> list[LogRecord]:
        with self._lock:
            return list(self._records)


class FileHandler(Handler):
    """One lock per file descriptor: concurrent writers append whole lines, never halves."""

    def __init__(
        self,
        path: str,
        fs: FileSystem | None = None,
        formatter: Formatter | None = None,
        level: LogLevel = LogLevel.NOTSET,
    ) -> None:
        super().__init__(formatter, level, name=f"file:{path}")
        self._fs: FileSystem = fs or LocalFileSystem()
        self._path = path
        self._lock = threading.Lock()
        self._stream = self._fs.open_append(path)
        self._written = self._fs.size(path)

    @property
    def path(self) -> str:
        return self._path

    def emit(self, record: LogRecord) -> None:
        line = self.formatter.format(record) + "\n"
        with self._lock:
            self._write_locked(line)

    def _write_locked(self, line: str) -> None:
        self._stream.write(line)
        self._written += len(line)

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            self._stream.flush()
            self._stream.close()


class RotatingFileHandler(FileHandler):
    """Size-based rotation *inside* the write lock, so no record is split by a rename."""

    def __init__(
        self,
        path: str,
        max_bytes: int,
        backup_count: int = 3,
        fs: FileSystem | None = None,
        formatter: Formatter | None = None,
        level: LogLevel = LogLevel.NOTSET,
    ) -> None:
        super().__init__(path, fs, formatter, level)
        if max_bytes <= 0 or backup_count < 1:
            raise ValueError("max_bytes must be positive and backup_count at least 1")
        self.max_bytes, self.backup_count = max_bytes, backup_count
        self.rotations = 0

    def _write_locked(self, line: str) -> None:
        if self._written and self._written + len(line) > self.max_bytes:
            self._rotate_locked()
        super()._write_locked(line)

    def _rotate_locked(self) -> None:
        self._stream.flush()
        self._stream.close()
        oldest = f"{self._path}.{self.backup_count}"
        if self._fs.exists(oldest):
            self._fs.remove(oldest)
        for index in range(self.backup_count - 1, 0, -1):
            src = f"{self._path}.{index}"
            if self._fs.exists(src):
                self._fs.rename(src, f"{self._path}.{index + 1}")
        self._fs.rename(self._path, f"{self._path}.1")
        self._stream = self._fs.open_append(self._path)
        self._written = 0
        self.rotations += 1


class RemoteHandler(Handler):
    """Ships batches over an injected transport; a failing transport raises to the manager."""

    def __init__(
        self,
        transport: Transport,
        batch_size: int = 10,
        formatter: Formatter | None = None,
        level: LogLevel = LogLevel.NOTSET,
    ) -> None:
        super().__init__(formatter, level, name="remote")
        self._transport = transport
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._buffer: list[str] = []

    def emit(self, record: LogRecord) -> None:
        line = self.formatter.format(record)
        with self._lock:
            self._buffer.append(line)
            batch = self._buffer[:] if len(self._buffer) >= self.batch_size else []
            if batch:
                self._buffer.clear()
        if batch:
            self._transport.send(batch)

    def flush(self) -> None:
        with self._lock:
            batch, self._buffer = self._buffer[:], []
        if batch:
            self._transport.send(batch)


# --8<-- [end:concrete_handlers]


# --8<-- [start:async_handler]
class AsyncHandler(Handler):
    """Decorator: takes any handler off the request thread onto one worker thread.

    The queue is bounded on purpose. An unbounded queue turns a logging burst
    into an out-of-memory kill; a bounded queue forces you to answer the only
    question that matters -- block the caller, or drop records and count them.
    """

    def __init__(
        self,
        inner: Handler,
        capacity: int = 1024,
        policy: OverflowPolicy = OverflowPolicy.DROP_NEWEST,
        block_timeout: float | None = None,
        on_error: Callable[[Handler, LogRecord, Exception], None] | None = None,
    ) -> None:
        super().__init__(inner.formatter, inner.level, name=f"async:{inner.name}")
        self.inner = inner
        self.policy = policy
        self.block_timeout = block_timeout
        self._queue: queue.Queue[LogRecord | None] = queue.Queue(maxsize=capacity)
        self._on_error = on_error
        self._state_lock = threading.Lock()
        self._state = WorkerState.IDLE
        self._thread: threading.Thread | None = None
        self._drop_lock = threading.Lock()
        self.dropped = 0

    @property
    def state(self) -> WorkerState:
        with self._state_lock:
            return self._state

    def start(self) -> AsyncHandler:
        with self._state_lock:
            if self._state is not WorkerState.IDLE:
                return self
            self._state = WorkerState.RUNNING
            self._thread = threading.Thread(target=self._run, name=f"log-{self.inner.name}", daemon=True)
            self._thread.start()
        return self

    def emit(self, record: LogRecord) -> None:
        if self.policy is OverflowPolicy.BLOCK:
            try:
                self._queue.put(record, timeout=self.block_timeout)
            except queue.Full as exc:
                raise QueueOverflowError(f"{self.name} queue full after {self.block_timeout}s") from exc
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._shed(record)

    def _shed(self, record: LogRecord) -> None:
        """Count what was actually lost: the evicted record, the new one, or both.

        Both halves of ``DROP_OLDEST`` can race. If the queue drained between the
        overflow and the eviction there is nothing to evict, and if a producer
        refilled the freed slot the new record cannot take it. Counting the
        outcome rather than assuming one loss keeps ``dropped`` honest, which is
        the whole point of a bounded queue.
        """
        lost = 0
        if self.policy is OverflowPolicy.DROP_OLDEST:
            try:
                self._queue.get_nowait()
                self._queue.task_done()  # keep join() accounting correct
            except queue.Empty:
                pass
            else:
                lost += 1  # the evicted record is gone whatever happens next
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                lost += 1  # a racing producer took the slot we just freed
        else:
            lost += 1  # DROP_NEWEST: the record never enters the queue
        if lost:
            with self._drop_lock:
                self.dropped += lost

    def _run(self) -> None:
        while True:
            record = self._queue.get()
            if record is None:
                self._queue.task_done()
                return
            try:
                self.inner.handle(record)
            except Exception as exc:  # one bad record must not kill the worker
                if self._on_error is not None:
                    self._on_error(self.inner, record, exc)
            finally:
                self._queue.task_done()

    def flush(self) -> None:
        """Deterministic barrier: return once the worker has drained the backlog."""
        if self.state is WorkerState.RUNNING:
            self._queue.join()
        self.inner.flush()

    def close(self) -> None:
        with self._state_lock:
            if self._state is not WorkerState.RUNNING:
                self._state = WorkerState.STOPPED
                self.inner.close()
                return
            self._state = WorkerState.DRAINING
            thread = self._thread
        self._queue.put(None)
        if thread is not None:
            thread.join()
        self.inner.close()
        with self._state_lock:
            self._state = WorkerState.STOPPED


# --8<-- [end:async_handler]
