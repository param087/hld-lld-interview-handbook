"""Messages, offsets, policies and errors. No locks and no threads live here.

The identity of a stored message is ``(topic, partition, offset)``. Everything
else in the package -- ordering, replay, at-least-once delivery -- is a
consequence of that triple being stable and monotonic per partition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError


# --8<-- [start:enums]
class DeliveryState(StrEnum):
    """The lifecycle of one attempt to hand a record to one consumer group."""

    PENDING = "pending"  # appended to the partition, nobody has read it yet
    IN_FLIGHT = "in_flight"  # a worker is inside consumer.on_message
    ACKED = "acked"  # returned normally; the offset moves past it
    RETRY_SCHEDULED = "retry_scheduled"  # raised, attempts left, waiting out the backoff
    DEAD_LETTERED = "dead_lettered"  # attempts exhausted; parked in the DLQ, offset moves on


class BrokerState(StrEnum):
    RUNNING = "running"
    DRAINING = "draining"  # close() called: no new publishes, workers finishing
    STOPPED = "stopped"


class FullPolicy(StrEnum):
    """What a partition does when its bounded buffer is full."""

    BLOCK = "block"  # backpressure: the producer waits for the slowest group
    DROP_OLDEST = "drop_oldest"  # shed: the newest data matters more than the oldest


# --8<-- [end:enums]


# --8<-- [start:errors]
class TopicNotFoundError(NotFoundError):
    """Publish or subscribe to a topic that was never created."""


class TopicExistsError(ConflictError):
    """create_topic called twice with the same name."""


class BrokerClosedError(InvalidStateError):
    """The broker is draining or stopped; it no longer accepts work."""


class BackpressureError(ConflictError):
    """A bounded partition stayed full for the whole publish timeout."""


class OffsetOutOfRangeError(ValidationError):
    """The requested offset was trimmed by retention, or is beyond the log end."""


class SubscriptionError(ValidationError):
    """An impossible subscription (unknown topic, duplicate consumer name, empty group)."""


# --8<-- [end:errors]


# --8<-- [start:messages]
@dataclass(frozen=True, slots=True)
class Message:
    """What a producer hands to the broker. The key decides the partition."""

    id: str
    topic: str
    payload: str
    key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    created: float = 0.0  # epoch seconds from the injected Clock


@dataclass(frozen=True, slots=True)
class Record:
    """A message once it has a place in the log. Immutable, shared by every group."""

    topic: str
    partition: int
    offset: int
    message: Message

    @property
    def key(self) -> str | None:
        return self.message.key

    @property
    def payload(self) -> str:
        return self.message.payload

    def __str__(self) -> str:
        return f"{self.topic}/{self.partition}@{self.offset} {self.payload}"


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """A record that exhausted its retries, plus why."""

    record: Record
    group: str
    attempts: int
    error: str
    failed_at: float


# --8<-- [end:messages]


# --8<-- [start:policies]
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with a ceiling. Deterministic: no jitter in the handbook build."""

    max_attempts: int = 3
    base_delay: float = 0.001
    multiplier: float = 2.0
    max_delay: float = 0.05

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay < 0:
            raise ValidationError("max_attempts must be >= 1 and base_delay >= 0")

    def should_retry(self, attempt: int) -> bool:
        return attempt < self.max_attempts

    def delay_for(self, attempt: int) -> float:
        return min(self.max_delay, self.base_delay * (self.multiplier ** (attempt - 1)))


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Two independent bounds: how many records a partition holds, and for how long.

    ``max_messages`` is the bounded buffer that creates backpressure; space is
    reclaimed when the slowest subscribed group commits past the oldest record.
    ``max_age_seconds`` trims regardless of consumers -- the reason a slow group
    can be fast-forwarded and lose records.
    """

    max_messages: int = 1024
    max_age_seconds: float | None = None
    on_full: FullPolicy = FullPolicy.BLOCK
    block_timeout: float = 1.0

    def __post_init__(self) -> None:
        if self.max_messages < 1:
            raise ValidationError("max_messages must be at least 1")


# --8<-- [end:policies]


# --8<-- [start:protocols]
class Consumer(Protocol):
    """Returning normally acks the record; raising nacks it and starts the retry clock."""

    name: str

    def on_message(self, record: Record) -> None: ...


class Partitioner(Protocol):
    """Strategy: which partition a key lands in. Must be stable across processes."""

    def partition_for(self, key: str | None, partition_count: int) -> int: ...


# --8<-- [end:protocols]
