"""A short scenario: inheritance, propagation, context, rotation, isolation, shutdown."""

import sys

from common import FakeClock, SequentialIdGenerator
from lld.logging_framework.formatters import JsonFormatter, PlainFormatter
from lld.logging_framework.handlers import (
    AsyncHandler,
    Handler,
    InMemoryHandler,
    RotatingFileHandler,
    StreamHandler,
)
from lld.logging_framework.models import HandlerFailure, LogLevel, LogRecord, OverflowPolicy
from lld.logging_framework.services import LogContext, LoggerConfigBuilder, LogManager
from lld.logging_framework.sinks import MemoryFileSystem

LOG_PATH = "/var/log/app.log"


class BrokenHandler(Handler):
    """A sink that always fails, to show that its siblings still run."""

    def __init__(self) -> None:
        super().__init__(PlainFormatter(), LogLevel.NOTSET, name="broken")

    def emit(self, record: LogRecord) -> None:
        raise HandlerFailure("disk full")


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    manager = LogManager(clock=clock, ids=SequentialIdGenerator("R"), root_level=LogLevel.INFO)
    files = MemoryFileSystem()

    console = StreamHandler(sys.stdout, PlainFormatter())
    audit = InMemoryHandler(JsonFormatter(), level=LogLevel.ERROR)
    rotating = RotatingFileHandler(LOG_PATH, max_bytes=200, backup_count=2, fs=files)
    async_file = AsyncHandler(rotating, capacity=64, policy=OverflowPolicy.DROP_OLDEST).start()

    LoggerConfigBuilder(manager).logger("root").handler(console).handler(audit).apply()
    LoggerConfigBuilder(manager).logger("app.api").level(LogLevel.DEBUG).handler(async_file).apply()
    api = manager.get_logger("app.api")
    db = manager.get_logger("app.db")

    print(f"app.api level={api.effective_level()} (its own)  app.db level={db.effective_level()} (inherited)")
    db.debug("connection pool warm")  # below the inherited INFO threshold: never allocated
    with LogContext.bind(correlation_id="c-42", user="u-7"):
        api.info("GET /orders", route="/orders")
        clock.advance(1)
        api.debug("cache miss", key="orders:u-7")
        clock.advance(1)
        api.error("upstream timeout", upstream="billing")

    async_file.flush()
    print(f"rotating file wrote {len(files.lines(LOG_PATH))} live line(s), {rotating.rotations} rotation(s)")
    print(f"files on disk: {', '.join(files.paths())}")
    print(f"audit sink (ERROR and above) captured {len(audit.lines())}: {audit.lines()[0]}")

    api.add_handler(BrokenHandler())
    clock.advance(1)
    api.warning("degraded mode")
    print(f"manager errors after a failing handler: {manager.errors()}")

    manager.shutdown()
    print(f"async worker state={async_file.state}, dropped={async_file.dropped}")
    print(f"unhandled records (no handler anywhere): {manager.unhandled_count()}")


if __name__ == "__main__":
    main()
