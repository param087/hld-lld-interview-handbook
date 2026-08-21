"""The hierarchy, the registry and the fluent config: where propagation happens."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import ClassVar

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.logging_framework.handlers import Handler
from lld.logging_framework.models import (
    Filter,
    LoggerShutdownError,
    LoggingConfigError,
    LogLevel,
    LogRecord,
)

ROOT_NAME = "root"
DEFAULT_ROOT_LEVEL = LogLevel.WARNING
_EMPTY_CONTEXT: Mapping[str, str] = MappingProxyType({})


# --8<-- [start:context]
class LogContext:
    """Ambient structured context (correlation id, user id, tenant).

    A ``ContextVar`` is per-thread *and* per-task, so a request handler binds
    once and every log line inside it carries the id without threading an extra
    argument through ten call frames.
    """

    _var: ClassVar[ContextVar[Mapping[str, str]]] = ContextVar(
        "handbook_log_context", default=_EMPTY_CONTEXT
    )

    @classmethod
    def current(cls) -> dict[str, str]:
        return dict(cls._var.get())

    @classmethod
    @contextmanager
    def bind(cls, **values: str) -> Iterator[None]:
        token = cls._var.set(MappingProxyType({**cls._var.get(), **values}))
        try:
            yield
        finally:
            cls._var.reset(token)


# --8<-- [end:context]


# --8<-- [start:logger]
class Logger:
    """A node in the dotted hierarchy: ``app.api.auth`` is a child of ``app.api``.

    Two things travel up the chain, and they travel differently:

    * the **threshold** is inherited -- ``effective_level`` walks up until it
      finds a logger whose level is not ``NOTSET``;
    * the **record** is propagated -- every ancestor's handlers get a turn,
      until a logger with ``propagate=False`` stops the chain.
    """

    def __init__(
        self,
        name: str,
        manager: LogManager,
        level: LogLevel = LogLevel.NOTSET,
        propagate: bool = True,
    ) -> None:
        self.name = name
        self.level = level
        self.propagate = propagate
        self.parent: Logger | None = None
        self._manager = manager
        self._lock = threading.RLock()  # guards the handler and filter lists
        self._handlers: list[Handler] = []
        self._filters: list[Filter] = []

    def add_handler(self, handler: Handler) -> Logger:
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)
        return self

    def remove_handler(self, handler: Handler) -> Logger:
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)
        return self

    def clear_handlers(self) -> Logger:
        with self._lock:
            self._handlers.clear()
        return self

    def handlers(self) -> list[Handler]:
        with self._lock:
            return list(self._handlers)

    def add_filter(self, log_filter: Filter) -> Logger:
        with self._lock:
            self._filters.append(log_filter)
        return self

    def effective_level(self) -> LogLevel:
        node: Logger | None = self
        while node is not None:
            if node.level is not LogLevel.NOTSET:
                return node.level
            node = node.parent
        return DEFAULT_ROOT_LEVEL

    def is_enabled_for(self, level: LogLevel) -> bool:
        return level >= self.effective_level()

    def log(self, level: LogLevel, message: str, **context: object) -> LogRecord | None:
        """The single entry point. Returns the record when it was emitted, else None."""
        if not self.is_enabled_for(level):
            return None  # the cheap path: one integer comparison, no record allocated
        record = self._manager.make_record(self.name, level, message, context)
        with self._lock:
            filters = tuple(self._filters)
        if not all(f.allows(record) for f in filters):
            return None
        self._dispatch(record)
        return record

    def debug(self, message: str, **context: object) -> LogRecord | None:
        return self.log(LogLevel.DEBUG, message, **context)

    def info(self, message: str, **context: object) -> LogRecord | None:
        return self.log(LogLevel.INFO, message, **context)

    def warning(self, message: str, **context: object) -> LogRecord | None:
        return self.log(LogLevel.WARNING, message, **context)

    def error(self, message: str, **context: object) -> LogRecord | None:
        return self.log(LogLevel.ERROR, message, **context)

    def critical(self, message: str, **context: object) -> LogRecord | None:
        return self.log(LogLevel.CRITICAL, message, **context)

    def _dispatch(self, record: LogRecord) -> int:
        """Chain of Responsibility: every ancestor gets a turn unless one stops the chain."""
        node: Logger | None = self
        written = 0
        while node is not None:
            written += node._emit_local(record)
            if not node.propagate:
                break
            node = node.parent
        if written == 0:
            self._manager.on_unhandled(record)
        return written

    def _emit_local(self, record: LogRecord) -> int:
        """Run this logger's own handlers. A raising handler never stops its siblings."""
        written = 0
        for handler in self.handlers():  # snapshot: a handler added mid-loop waits for the next record
            try:
                if handler.handle(record):
                    written += 1
            except Exception as exc:  # failure isolation is the whole point
                self._manager.on_handler_error(self.name, handler, record, exc)
        return written


# --8<-- [end:logger]


# --8<-- [start:manager]
class LogManager:
    """The registry: owns the root logger, mints records, and shuts handlers down.

    This is the one place a Singleton is defensible -- a second registry would
    mean two roots and two shutdown paths for one process. ``instance()`` is the
    process-wide accessor; the constructor stays public so every test builds its
    own isolated manager instead of fighting global state.
    """

    _instance_lock: ClassVar[threading.Lock] = threading.Lock()
    _instance: ClassVar[LogManager | None] = None

    def __init__(
        self,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        root_level: LogLevel = DEFAULT_ROOT_LEVEL,
    ) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("R")
        self._lock = threading.RLock()  # guards the logger registry
        self._diagnostics_lock = threading.Lock()  # guards the error log and counters
        self.root = Logger(ROOT_NAME, self, level=root_level)
        self._loggers: dict[str, Logger] = {ROOT_NAME: self.root}
        self._errors: list[tuple[str, str]] = []
        self._unhandled = 0
        self._closed = False

    @classmethod
    def instance(cls) -> LogManager:
        """Double-checked locking: the fast path never takes the lock."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._instance_lock:
            cls._instance = None

    def get_logger(self, name: str) -> Logger:
        """Factory: idempotent, and it creates the whole ancestor chain eagerly."""
        if not name or name.strip() != name or ".." in name or name.startswith(".") or name.endswith("."):
            raise LoggingConfigError(f"invalid logger name: {name!r}")
        with self._lock:
            if self._closed:
                raise LoggerShutdownError("log manager is shut down")
            return self._get_locked(name)

    def _get_locked(self, name: str) -> Logger:
        existing = self._loggers.get(name)
        if existing is not None:
            return existing
        logger = Logger(name, self)
        # Build parents first, so a late `get_logger("app")` never has to re-parent
        # an already-created `app.api` (CPython solves this with PlaceHolder nodes).
        logger.parent = self.root if "." not in name else self._get_locked(name.rsplit(".", 1)[0])
        self._loggers[name] = logger
        return logger

    def loggers(self) -> list[Logger]:
        with self._lock:
            return list(self._loggers.values())

    def make_record(self, logger_name: str, level: LogLevel, message: str, context: Mapping[str, object]) -> LogRecord:
        merged = LogContext.current()
        merged.update({key: str(value) for key, value in context.items()})
        return LogRecord(
            id=self._ids.next_id(),
            logger_name=logger_name,
            level=level,
            message=message,
            created=self._clock.now(),
            thread_name=threading.current_thread().name,
            context=merged,
        )

    def on_handler_error(self, logger_name: str, handler: Handler, record: LogRecord, exc: Exception) -> None:
        """Never raise into the caller: an app must not crash because a disk filled up."""
        with self._diagnostics_lock:
            self._errors.append((handler.name, f"{type(exc).__name__}: {exc}"))

    def on_unhandled(self, record: LogRecord) -> None:
        with self._diagnostics_lock:
            self._unhandled += 1

    def errors(self) -> list[tuple[str, str]]:
        with self._diagnostics_lock:
            return list(self._errors)

    def unhandled_count(self) -> int:
        with self._diagnostics_lock:
            return self._unhandled

    def shutdown(self) -> None:
        """Flush every handler exactly once, even when several loggers share one."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            unique: dict[int, Handler] = {
                id(handler): handler for logger in self._loggers.values() for handler in logger.handlers()
            }
        for handler in unique.values():
            try:
                handler.close()
            except Exception as exc:  # shutdown must reach every handler
                with self._diagnostics_lock:
                    self._errors.append((handler.name, f"close failed: {type(exc).__name__}: {exc}"))


# --8<-- [end:manager]


# --8<-- [start:builder]
class LoggerConfigBuilder:
    """Fluent configuration. ``apply()`` is the only method that mutates a logger.

    Configuring logging is the textbook Builder case: many optional knobs, an
    invalid half-configured state you must not expose, and a validation step
    that belongs in one place rather than in every caller.
    """

    def __init__(self, manager: LogManager) -> None:
        self._manager = manager
        self._name = ROOT_NAME
        self._level: LogLevel | None = None
        self._propagate: bool | None = None
        self._replace = False
        self._handlers: list[Handler] = []
        self._filters: list[Filter] = []

    def logger(self, name: str) -> LoggerConfigBuilder:
        self._name = name
        return self

    def level(self, level: LogLevel) -> LoggerConfigBuilder:
        if not isinstance(level, LogLevel):
            raise LoggingConfigError(f"level must be a LogLevel, got {level!r}")
        self._level = level
        return self

    def propagate(self, enabled: bool) -> LoggerConfigBuilder:
        self._propagate = enabled
        return self

    def handler(self, handler: Handler) -> LoggerConfigBuilder:
        self._handlers.append(handler)
        return self

    def filter(self, log_filter: Filter) -> LoggerConfigBuilder:
        self._filters.append(log_filter)
        return self

    def replace_handlers(self) -> LoggerConfigBuilder:
        """Drop whatever was attached before applying, for config reload."""
        self._replace = True
        return self

    def apply(self) -> Logger:
        if self._replace and not self._handlers and self._propagate is False:
            raise LoggingConfigError(f"{self._name} would have no handlers and no propagation")
        logger = self._manager.get_logger(self._name)
        if self._replace:
            for handler in logger.handlers():
                handler.close()
            logger.clear_handlers()
        if self._level is not None:
            logger.level = self._level
        if self._propagate is not None:
            logger.propagate = self._propagate
        for handler in self._handlers:
            logger.add_handler(handler)
        for log_filter in self._filters:
            logger.add_filter(log_filter)
        return logger


# --8<-- [end:builder]
