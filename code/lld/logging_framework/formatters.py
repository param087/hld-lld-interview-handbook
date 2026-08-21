"""How a record is rendered. One half of the Bridge; ``handlers.py`` is the other."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Protocol

from lld.logging_framework.models import LogRecord


# --8<-- [start:formatters]
class Formatter(Protocol):
    """Anything that turns a record into a line. No inheritance required."""

    def format(self, record: LogRecord) -> str: ...


def _timestamp(created: float) -> str:
    """The record carries the time; the formatter only renders it."""
    return datetime.fromtimestamp(created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class PlainFormatter:
    """Human-readable, column-aligned, context appended as ``key=value`` pairs."""

    def __init__(self, show_thread: bool = False) -> None:
        self.show_thread = show_thread

    def format(self, record: LogRecord) -> str:
        head = f"{_timestamp(record.created)} {record.level!s:<8} {record.logger_name:<12}"
        if self.show_thread:
            head = f"{head} [{record.thread_name}]"
        tail = " ".join(f"{k}={v}" for k, v in sorted(record.context.items()))
        return f"{head} {record.message}{' ' + tail if tail else ''}"


class JsonFormatter:
    """One JSON object per line: what a log shipper actually wants to parse."""

    def __init__(self, sort_keys: bool = True) -> None:
        self.sort_keys = sort_keys

    def format(self, record: LogRecord) -> str:
        payload = {
            "ts": _timestamp(record.created),
            "level": str(record.level),
            "logger": record.logger_name,
            "msg": record.message,
            **record.context,
        }
        return json.dumps(payload, sort_keys=self.sort_keys, separators=(",", ":"))


# --8<-- [end:formatters]
