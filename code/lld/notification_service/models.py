"""Requests, notifications, templates and preferences.

Two ideas to hold on to while reading: a ``NotificationRequest`` is what a
caller asks for, a ``Notification`` is one delivery attempt chain on one
channel; and the idempotency key travels from the request all the way to the
delivery ledger, because that is what turns at-least-once into effectively-once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError

PLACEHOLDER = re.compile(r"\{(\w+)\}")


# --8<-- [start:enums]
class Channel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    IN_APP = "in_app"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Higher wins. The queue orders by due time first, then by this."""
        return {"low": 0, "normal": 1, "high": 2, "critical": 3}[self.value]

    @property
    def bypasses_rate_limit(self) -> bool:
        """A password reset is never dropped because you posted too much today."""
        return self is Priority.CRITICAL


class DeliveryStatus(StrEnum):
    QUEUED = "queued"  # accepted, waiting for a worker
    SENDING = "sending"  # claimed by a worker, provider call in flight
    RETRYING = "retrying"  # the attempt failed, backoff is running
    DELIVERED = "delivered"
    SUPPRESSED = "suppressed"  # preferences changed while it sat in the queue
    DEAD_LETTER = "dead_letter"  # every channel and every attempt exhausted


class SuppressionReason(StrEnum):
    OPTED_OUT = "opted_out"
    NO_CHANNEL = "no_channel"
    DUPLICATE = "duplicate"
    RATE_LIMITED = "rate_limited"


class CircuitState(StrEnum):
    CLOSED = "closed"  # calls flow through
    OPEN = "open"  # provider is failing; fail fast without calling it
    HALF_OPEN = "half_open"  # cooldown elapsed; let one probe through


# --8<-- [end:enums]


# --8<-- [start:errors]
class TemplateNotFoundError(NotFoundError):
    """No template registered for this (event, channel) pair."""


class RenderError(ValidationError):
    """The payload does not carry every placeholder the template needs."""


class ChannelUnavailableError(ConflictError):
    """The provider refused, or its circuit breaker is open."""


class QueueFullError(ConflictError):
    """Backpressure: the bounded queue is at capacity."""


class NotificationStateError(InvalidStateError):
    """The notification's status forbids this transition."""


# --8<-- [end:errors]


# --8<-- [start:values]
@dataclass(frozen=True, slots=True)
class Template:
    """Subject and body with ``{placeholder}`` slots, per event and channel."""

    id: str
    event: str
    channel: Channel
    subject: str
    body: str

    def placeholders(self) -> frozenset[str]:
        return frozenset(PLACEHOLDER.findall(self.subject) + PLACEHOLDER.findall(self.body))


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    """What a sender receives. Frozen: a provider stub cannot mutate the domain."""

    notification_id: str
    channel: Channel
    recipient: str
    subject: str
    body: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class UserPreferences:
    """Opt-outs, the fallback order and the addresses, in one value object."""

    user_id: str
    channel_order: tuple[Channel, ...] = (Channel.PUSH, Channel.EMAIL, Channel.SMS)
    muted_events: frozenset[str] = frozenset()
    disabled_channels: frozenset[Channel] = frozenset()
    addresses: dict[Channel, str] = field(default_factory=dict)

    def allows(self, event: str, channel: Channel) -> bool:
        return (
            event not in self.muted_events
            and channel not in self.disabled_channels
            and channel in self.addresses
        )

    def ordered_channels(self, event: str) -> list[Channel]:
        """The fallback chain: preferred first, unusable channels removed."""
        return [c for c in self.channel_order if self.allows(event, c)]

    def muting(self, event: str) -> UserPreferences:
        return UserPreferences(
            self.user_id,
            self.channel_order,
            self.muted_events | {event},
            self.disabled_channels,
            dict(self.addresses),
        )


@dataclass(frozen=True, slots=True)
class NotificationRequest:
    """What a caller asks for. Immutable, and carries the idempotency key."""

    id: str
    user_id: str
    event: str
    payload: dict[str, str] = field(default_factory=dict)
    priority: Priority = Priority.NORMAL
    idempotency_key: str = ""
    requested_at: float = 0.0
    send_after: float = 0.0  # scheduled sends: not before this instant
    channels: tuple[Channel, ...] = ()  # explicit override of the preference order

    def dedup_key(self) -> str:
        """Falls back to (user, event) so a caller that forgets a key still dedups."""
        return self.idempotency_key or f"{self.user_id}:{self.event}"


@dataclass(frozen=True, slots=True)
class Suppression:
    channel: Channel | None
    reason: SuppressionReason
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    attempt: int
    channel: Channel
    at: float
    ok: bool
    error: str = ""


# --8<-- [end:values]


# --8<-- [start:notification]
@dataclass(slots=True)
class Notification:
    """One delivery chain: a channel, its fallbacks, and the attempts so far."""

    id: str
    request_id: str
    user_id: str
    event: str
    channel: Channel
    message: RenderedMessage
    payload: dict[str, str] = field(default_factory=dict)  # kept so a fallback can re-render
    priority: Priority = Priority.NORMAL
    status: DeliveryStatus = DeliveryStatus.QUEUED
    attempts: int = 0
    due_at: float = 0.0
    fallbacks: tuple[Channel, ...] = ()
    history: list[DeliveryAttempt] = field(default_factory=list)
    provider_message_id: str | None = None
    last_error: str = ""

    def claim(self) -> None:
        if self.status not in (DeliveryStatus.QUEUED, DeliveryStatus.RETRYING):
            raise NotificationStateError(f"{self.id} is {self.status}, not claimable")
        self.status = DeliveryStatus.SENDING
        self.attempts += 1

    def succeed(self, provider_message_id: str, at: float) -> None:
        self.status = DeliveryStatus.DELIVERED
        self.provider_message_id = provider_message_id
        self.history.append(DeliveryAttempt(self.attempts, self.channel, at, ok=True))

    def fail(self, error: str, at: float) -> None:
        self.last_error = error
        self.history.append(DeliveryAttempt(self.attempts, self.channel, at, ok=False, error=error))

    def schedule_retry(self, due_at: float) -> None:
        self.status = DeliveryStatus.RETRYING
        self.due_at = due_at

    def switch_to(self, channel: Channel, message: RenderedMessage, due_at: float) -> None:
        """Fallback: same notification, next channel, attempt counter reset."""
        self.channel = channel
        self.message = message
        self.fallbacks = tuple(c for c in self.fallbacks if c is not channel)
        self.attempts = 0
        self.status = DeliveryStatus.QUEUED
        self.due_at = due_at

    def dead_letter(self) -> None:
        self.status = DeliveryStatus.DEAD_LETTER

    def suppress(self) -> None:
        self.status = DeliveryStatus.SUPPRESSED

    def summary(self) -> str:
        return f"{self.id} {self.event} via {self.channel}: {self.status} after {self.attempts} attempt(s)"


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """What ``notify`` answers: what was accepted and what was dropped, and why."""

    request: NotificationRequest
    notification: Notification | None
    suppressions: tuple[Suppression, ...] = ()

    def accepted(self) -> bool:
        return self.notification is not None

    def line(self) -> str:
        if self.notification is not None:
            return f"{self.request.event} -> {self.notification.channel} ({self.notification.status})"
        reasons = ", ".join(s.reason for s in self.suppressions)
        return f"{self.request.event} -> suppressed ({reasons})"


# --8<-- [end:notification]
