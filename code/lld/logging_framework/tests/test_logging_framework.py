import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, SequentialIdGenerator
from lld.logging_framework.filters import RateLimitFilter
from lld.logging_framework.formatters import JsonFormatter, PlainFormatter
from lld.logging_framework.handlers import (
    AsyncHandler,
    FileHandler,
    Handler,
    InMemoryHandler,
    NullHandler,
    RemoteHandler,
    RotatingFileHandler,
    StreamHandler,
)
from lld.logging_framework.models import (
    HandlerFailure,
    LoggingConfigError,
    LogLevel,
    LogRecord,
    OverflowPolicy,
    QueueOverflowError,
    WorkerState,
)
from lld.logging_framework.services import LogContext, LoggerConfigBuilder, LogManager
from lld.logging_framework.sinks import MemoryFileSystem


class BrokenHandler(Handler):
    def __init__(self) -> None:
        super().__init__(PlainFormatter(), LogLevel.NOTSET, name="broken")

    def emit(self, record: LogRecord) -> None:
        raise HandlerFailure("disk full")


class CollectingTransport:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def send(self, lines: list[str]) -> None:
        self.batches.append(list(lines))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_700_000_000)


@pytest.fixture
def manager(clock: FakeClock) -> LogManager:
    return LogManager(clock=clock, ids=SequentialIdGenerator("R"), root_level=LogLevel.INFO)


# --8<-- [start:hierarchy]
def test_record_climbs_the_hierarchy_and_levels_are_inherited(manager: LogManager) -> None:
    root_sink, api_sink = InMemoryHandler(), InMemoryHandler()
    manager.root.add_handler(root_sink)
    LoggerConfigBuilder(manager).logger("app.api").level(LogLevel.DEBUG).handler(api_sink).apply()
    auth = manager.get_logger("app.api.auth")

    assert auth.effective_level() is LogLevel.DEBUG  # inherited from app.api
    assert manager.get_logger("app.db").effective_level() is LogLevel.INFO  # inherited from root

    auth.debug("token refreshed")
    assert len(api_sink.lines()) == 1  # the ancestor's handler ran
    assert len(root_sink.lines()) == 1  # ...and so did the root's
    assert manager.get_logger("app.db").debug("noisy") is None  # below the inherited threshold


# --8<-- [end:hierarchy]


def test_propagate_false_stops_the_chain(manager: LogManager) -> None:
    root_sink, audit_sink = InMemoryHandler(), InMemoryHandler()
    manager.root.add_handler(root_sink)
    audit = (
        LoggerConfigBuilder(manager)
        .logger("app.audit")
        .level(LogLevel.INFO)
        .propagate(False)
        .handler(audit_sink)
        .apply()
    )
    audit.info("user deleted")
    assert len(audit_sink.lines()) == 1 and root_sink.lines() == []


@pytest.mark.parametrize("name", ["", " app", "app.", ".app", "app..api"])
def test_invalid_logger_names_are_rejected(manager: LogManager, name: str) -> None:
    with pytest.raises(LoggingConfigError):
        manager.get_logger(name)


def test_builder_rejects_a_non_level_and_a_dead_end_config(manager: LogManager) -> None:
    with pytest.raises(LoggingConfigError):
        LoggerConfigBuilder(manager).logger("app").level("DEBUG")  # type: ignore[arg-type]
    with pytest.raises(LoggingConfigError):
        LoggerConfigBuilder(manager).logger("app").replace_handlers().propagate(False).apply()


def test_async_handler_moves_idle_running_stopped_and_flush_is_a_barrier(manager: LogManager) -> None:
    sink = InMemoryHandler()
    async_handler = AsyncHandler(sink, capacity=8)
    assert async_handler.state is WorkerState.IDLE
    async_handler.start()
    assert async_handler.state is WorkerState.RUNNING

    manager.root.add_handler(async_handler)
    log = manager.get_logger("app.worker")
    for i in range(5):
        log.info(f"job-{i}")
    async_handler.flush()  # deterministic: returns only when the queue is drained
    assert [line.split()[-1] for line in sink.lines()] == [f"job-{i}" for i in range(5)]

    manager.shutdown()
    assert async_handler.state is WorkerState.STOPPED


@pytest.mark.parametrize(
    ("policy", "expected_tail"),
    [(OverflowPolicy.DROP_NEWEST, ["m0", "m1"]), (OverflowPolicy.DROP_OLDEST, ["m1", "m2"])],
)
def test_bounded_queue_sheds_load_and_counts_the_drops(
    manager: LogManager, policy: OverflowPolicy, expected_tail: list[str]
) -> None:
    sink = InMemoryHandler()
    async_handler = AsyncHandler(sink, capacity=2, policy=policy)  # worker not started: nothing drains
    manager.root.add_handler(async_handler)
    log = manager.get_logger("app.burst")
    for i in range(3):
        log.info(f"m{i}")

    assert async_handler.dropped == 1
    async_handler.start()
    async_handler.flush()
    assert [line.split()[-1] for line in sink.lines()] == expected_tail


def test_block_policy_applies_backpressure_until_it_times_out(manager: LogManager) -> None:
    async_handler = AsyncHandler(
        InMemoryHandler(), capacity=1, policy=OverflowPolicy.BLOCK, block_timeout=0.01
    )
    manager.root.add_handler(async_handler)
    log = manager.get_logger("app.slow")
    log.info("first")  # fills the one-slot queue; the worker was never started

    record = log.info("second")  # the caller waits, then gives up
    assert record is not None and async_handler.dropped == 0
    [(handler_name, message)] = manager.errors()
    assert handler_name == "async:memory" and message.startswith("QueueOverflowError")

    with pytest.raises(QueueOverflowError):  # the handler itself is explicit about it
        async_handler.handle(record)


# --8<-- [start:isolation]
def test_a_failing_handler_never_stops_its_siblings(manager: LogManager) -> None:
    good = InMemoryHandler()
    LoggerConfigBuilder(manager).logger("app.api").handler(BrokenHandler()).handler(good).apply()

    record = manager.get_logger("app.api").error("upstream timeout")

    assert record is not None  # the caller is not told; logging is best effort
    assert len(good.lines()) == 1  # the sibling handler still wrote
    assert manager.errors() == [("broken", "HandlerFailure: disk full")]


# --8<-- [end:isolation]


# --8<-- [start:concurrency]
def test_concurrent_writers_never_interleave_a_line(manager: LogManager) -> None:
    files = MemoryFileSystem()
    handler = FileHandler("/var/log/app.log", fs=files, formatter=PlainFormatter(show_thread=True))
    manager.root.add_handler(handler)
    log = manager.get_logger("app.api")

    def write(i: int) -> None:
        log.info(f"request-{i:03d}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(400)))
    handler.close()

    lines = files.lines("/var/log/app.log")
    assert len(lines) == 400  # one lock per file: no line is split or overwritten
    assert {line.split()[-1] for line in lines} == {f"request-{i:03d}" for i in range(400)}


# --8<-- [end:concurrency]


def test_rotation_happens_inside_the_write_lock(manager: LogManager) -> None:
    files = MemoryFileSystem()
    handler = RotatingFileHandler("/var/log/app.log", max_bytes=120, backup_count=2, fs=files)
    manager.root.add_handler(handler)
    log = manager.get_logger("app.api")
    for i in range(9):
        log.info(f"line-{i}")
    handler.close()

    assert handler.rotations == 4  # 50-byte lines, two per 120-byte generation
    assert files.paths() == ["/var/log/app.log", "/var/log/app.log.1", "/var/log/app.log.2"]
    assert [line.split()[-1] for line in files.lines("/var/log/app.log")] == ["line-8"]
    assert [line.split()[-1] for line in files.lines("/var/log/app.log.1")] == ["line-6", "line-7"]
    assert [line.split()[-1] for line in files.lines("/var/log/app.log.2")] == ["line-4", "line-5"]


def test_shutdown_drains_the_queue_before_closing_the_file(manager: LogManager) -> None:
    files = MemoryFileSystem()
    async_handler = AsyncHandler(FileHandler("/var/log/app.log", fs=files), capacity=256).start()
    manager.root.add_handler(async_handler)
    log = manager.get_logger("app.api")
    for i in range(50):
        log.info(f"job-{i}")

    manager.shutdown()
    assert len(files.lines("/var/log/app.log")) == 50  # nothing lost at exit


def test_context_is_bound_ambiently_and_json_formatting_keeps_it(manager: LogManager) -> None:
    sink = InMemoryHandler(JsonFormatter())
    manager.root.add_handler(sink)
    log = manager.get_logger("app.api")
    with LogContext.bind(correlation_id="c-42"):
        log.info("GET /orders", route="/orders")
    log.info("after the request")

    first, second = (json.loads(line) for line in sink.lines())
    assert first["correlation_id"] == "c-42" and first["route"] == "/orders"
    assert "correlation_id" not in second  # the binding was undone on exit


def test_rate_limit_filter_reopens_the_window_on_the_injected_clock(clock: FakeClock, manager: LogManager) -> None:
    sink = InMemoryHandler()
    limiter = RateLimitFilter(max_per_window=2, window_seconds=60, clock=clock)
    LoggerConfigBuilder(manager).logger("app.noisy").handler(sink).filter(limiter).apply()
    log = manager.get_logger("app.noisy")
    for _ in range(5):
        log.warning("retrying")
    assert len(sink.lines()) == 2 and limiter.suppressed == 3

    clock.advance(61)
    log.warning("retrying")
    assert len(sink.lines()) == 3


def test_null_handler_absorbs_records_and_unhandled_ones_are_counted(manager: LogManager) -> None:
    orphan = manager.get_logger("library.http")
    orphan.error("no handler anywhere")
    assert manager.unhandled_count() == 1

    orphan.add_handler(NullHandler())
    orphan.error("still nothing written, but nobody complains")
    assert manager.unhandled_count() == 1


def test_remote_handler_batches_and_flush_ships_the_remainder(manager: LogManager) -> None:
    transport = CollectingTransport()
    handler = RemoteHandler(transport, batch_size=3, formatter=JsonFormatter())
    manager.root.add_handler(handler)
    log = manager.get_logger("app.api")
    for i in range(7):
        log.info(f"event-{i}")
    assert [len(batch) for batch in transport.batches] == [3, 3]

    handler.flush()
    assert [len(batch) for batch in transport.batches] == [3, 3, 1]


def test_stream_handler_threshold_is_independent_of_the_logger(manager: LogManager) -> None:
    import io

    buffer = io.StringIO()
    manager.root.add_handler(StreamHandler(buffer, PlainFormatter(), level=LogLevel.ERROR))
    log = manager.get_logger("app.api")
    log.info("routine")
    log.error("boom")
    assert buffer.getvalue().count("\n") == 1 and "boom" in buffer.getvalue()
