"""Consumers used by the demo and the tests. Deterministic barriers, never sleeps."""

from __future__ import annotations

import threading

from common import HandbookError
from lld.pub_sub_system.models import Record


class ConsumerError(HandbookError):
    """Raised by a consumer to nack a record and start the retry clock."""


# --8<-- [start:consumers]
class RecordingConsumer:
    """Collects what it receives and lets a test wait for N records without sleeping."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._condition = threading.Condition()
        self._records: list[Record] = []

    def on_message(self, record: Record) -> None:
        with self._condition:
            self._records.append(record)
            self._condition.notify_all()

    def records(self) -> list[Record]:
        with self._condition:
            return list(self._records)

    def payloads(self) -> list[str]:
        return [r.payload for r in self.records()]

    def keys_in_order(self, key: str) -> list[str]:
        """Per-key ordering is the invariant partitioning buys you; this reads it back."""
        return [r.payload for r in self.records() if r.key == key]

    def wait_for(self, count: int, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: len(self._records) >= count, timeout=timeout)


class FlakyConsumer(RecordingConsumer):
    """Fails the first ``fail_times`` attempts for a payload, then succeeds.

    That is exactly the shape of a real transient failure, and it lets a test
    assert that retry works *and* that the record is delivered exactly once
    to the application after it finally succeeds.
    """

    def __init__(self, name: str, fail_times: int = 1, poison: str | None = None) -> None:
        super().__init__(name)
        self.fail_times = fail_times
        self.poison = poison
        self._attempts: dict[str, int] = {}
        self._attempt_lock = threading.Lock()

    def on_message(self, record: Record) -> None:
        with self._attempt_lock:
            seen = self._attempts.get(record.payload, 0) + 1
            self._attempts[record.payload] = seen
        if record.payload == self.poison:
            raise ConsumerError(f"cannot process {record.payload}")
        if seen <= self.fail_times:
            raise ConsumerError(f"transient failure {seen} on {record.payload}")
        super().on_message(record)

    def attempts(self, payload: str) -> int:
        with self._attempt_lock:
            return self._attempts.get(payload, 0)


# --8<-- [end:consumers]
