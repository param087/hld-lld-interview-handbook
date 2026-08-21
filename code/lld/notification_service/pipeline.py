"""The admission pipeline: preferences, dedup, rate limit, template, enqueue.

Every stage answers one question and may narrow ``ctx.channels`` or stop the
run. Adding quiet hours is a new stage in a list, not an ``if`` in a service.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from common import Clock, IdGenerator
from lld.notification_service.models import (
    PLACEHOLDER,
    Channel,
    Notification,
    NotificationRequest,
    RenderedMessage,
    RenderError,
    Suppression,
    SuppressionReason,
    Template,
    TemplateNotFoundError,
    UserPreferences,
)


# --8<-- [start:collaborators]
class TemplateEngine:
    """Registry plus renderer. A missing placeholder is an error, not an empty string."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._templates: dict[tuple[str, Channel], Template] = {}

    def register(self, template: Template) -> Template:
        with self._lock:
            self._templates[(template.event, template.channel)] = template
        return template

    def template_for(self, event: str, channel: Channel) -> Template:
        with self._lock:
            try:
                return self._templates[(event, channel)]
            except KeyError:
                raise TemplateNotFoundError(f"no {channel} template for event {event!r}") from None

    def has(self, event: str, channel: Channel) -> bool:
        with self._lock:
            return (event, channel) in self._templates

    def render(
        self,
        template: Template,
        notification_id: str,
        recipient: str,
        payload: dict[str, str],
        idempotency_key: str,
    ) -> RenderedMessage:
        missing = sorted(template.placeholders() - payload.keys())
        if missing:
            raise RenderError(f"template {template.id} needs {', '.join(missing)}")
        fill = PLACEHOLDER.sub(lambda m: str(payload[m.group(1)]), template.subject)
        body = PLACEHOLDER.sub(lambda m: str(payload[m.group(1)]), template.body)
        return RenderedMessage(notification_id, template.channel, recipient, fill, body, idempotency_key)


class DedupStore:
    """A sliding window on the *request* side: the same key twice inside it is one send."""

    def __init__(self, clock: Clock, window_seconds: float = 300.0) -> None:
        self._clock = clock
        self._window = window_seconds
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def claim(self, key: str) -> bool:
        """True the first time; False while an earlier claim is still inside the window."""
        now = self._clock.now()
        with self._lock:
            previous = self._seen.get(key)
            if previous is not None and now - previous < self._window:
                return False
            self._seen[key] = now
            return True

    def forget(self, key: str) -> None:
        with self._lock:
            self._seen.pop(key, None)


class TokenBucketRateLimiter:
    """Per-user budget. Refills continuously from the injected clock — no timer thread."""

    def __init__(self, clock: Clock, capacity: int = 5, refill_per_second: float = 0.2) -> None:
        self._clock = clock
        self._capacity = capacity
        self._refill = refill_per_second
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock.now()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self._capacity), now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True

    def tokens(self, key: str) -> float:
        with self._lock:
            return round(self._buckets.get(key, (float(self._capacity), 0.0))[0], 3)


# --8<-- [end:collaborators]


# --8<-- [start:pipeline]
@dataclass(slots=True)
class PipelineContext:
    """What flows through the stages. Stages narrow it; they never widen it."""

    request: NotificationRequest
    preferences: UserPreferences
    notification_id: str
    channels: list[Channel] = field(default_factory=list)
    messages: dict[Channel, RenderedMessage] = field(default_factory=dict)
    suppressions: list[Suppression] = field(default_factory=list)
    notification: Notification | None = None

    def drop(self, channel: Channel | None, reason: SuppressionReason, detail: str = "") -> None:
        self.suppressions.append(Suppression(channel, reason, detail))


class Stage(Protocol):
    """One question, asked once. ``False`` stops the pipeline."""

    name: str

    def process(self, ctx: PipelineContext) -> bool: ...


class NotificationSink(Protocol):
    """Whatever the last stage hands the notification to (the queue, in practice)."""

    def put(self, notification: Notification) -> None: ...


class PreferenceStage:
    """Opt-outs and the fallback order. First, because it drops the most traffic."""

    name = "preferences"

    def process(self, ctx: PipelineContext) -> bool:
        wanted = ctx.request.channels or tuple(ctx.preferences.channel_order)
        ctx.channels = [c for c in wanted if ctx.preferences.allows(ctx.request.event, c)]
        if ctx.channels:
            return True
        muted = ctx.request.event in ctx.preferences.muted_events
        ctx.drop(
            None,
            SuppressionReason.OPTED_OUT if muted else SuppressionReason.NO_CHANNEL,
            f"{ctx.request.user_id} has no usable channel for {ctx.request.event}",
        )
        return False


class DedupStage:
    """The same key inside the window is the same notification, not a second one."""

    name = "dedup"

    def __init__(self, store: DedupStore) -> None:
        self._store = store

    def process(self, ctx: PipelineContext) -> bool:
        if self._store.claim(ctx.request.dedup_key()):
            return True
        ctx.drop(None, SuppressionReason.DUPLICATE, ctx.request.dedup_key())
        return False


class RateLimitStage:
    """Per-user budget, which a CRITICAL request walks past."""

    name = "rate_limit"

    def __init__(self, limiter: TokenBucketRateLimiter) -> None:
        self._limiter = limiter

    def process(self, ctx: PipelineContext) -> bool:
        if ctx.request.priority.bypasses_rate_limit or self._limiter.allow(ctx.request.user_id):
            return True
        ctx.drop(None, SuppressionReason.RATE_LIMITED, ctx.request.user_id)
        return False


class TemplateStage:
    """Render each surviving channel. Deliberately *after* the three cheap filters:
    rendering is the expensive step and most drops happen before it."""

    name = "template"

    def __init__(self, engine: TemplateEngine) -> None:
        self._engine = engine

    def process(self, ctx: PipelineContext) -> bool:
        usable: list[Channel] = []
        for channel in ctx.channels:
            if not self._engine.has(ctx.request.event, channel):
                ctx.drop(channel, SuppressionReason.NO_CHANNEL, "no template")
                continue
            ctx.messages[channel] = self._engine.render(
                self._engine.template_for(ctx.request.event, channel),
                ctx.notification_id,
                ctx.preferences.addresses[channel],
                ctx.request.payload,
                ctx.request.dedup_key(),
            )
            usable.append(channel)
        ctx.channels = usable
        if usable:
            return True
        ctx.drop(None, SuppressionReason.NO_CHANNEL, "no template for any allowed channel")
        return False


class EnqueueStage:
    """Build one notification for the preferred channel; the rest become fallbacks."""

    name = "enqueue"

    def __init__(self, queue: NotificationSink, clock: Clock, ids: IdGenerator) -> None:
        self._queue = queue
        self._clock = clock
        self._ids = ids

    def process(self, ctx: PipelineContext) -> bool:
        primary, *fallbacks = ctx.channels
        notification = Notification(
            id=ctx.notification_id,
            request_id=ctx.request.id,
            user_id=ctx.request.user_id,
            event=ctx.request.event,
            channel=primary,
            message=ctx.messages[primary],
            payload=dict(ctx.request.payload),
            priority=ctx.request.priority,
            due_at=max(self._clock.now(), ctx.request.send_after),
            fallbacks=tuple(fallbacks),
        )
        ctx.notification = notification
        self._queue.put(notification)
        return True


class Pipeline:
    """Runs the stages in order and stops at the first ``False``."""

    def __init__(self, stages: Sequence[Stage]) -> None:
        self._stages = tuple(stages)

    def names(self) -> list[str]:
        return [s.name for s in self._stages]

    def run(self, ctx: PipelineContext) -> PipelineContext:
        for stage in self._stages:
            if not stage.process(ctx):
                return ctx
        return ctx


# --8<-- [end:pipeline]
