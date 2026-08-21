"""The bounded queue, the delivery ledger, the dispatcher and the facade.

Read them in this order. ``NotificationQueue`` is the backpressure boundary,
``DeliveryLedger`` is the idempotency guard that makes at-least-once safe,
``Dispatcher`` is the worker loop with retry, fallback and the dead letter
queue, and ``NotificationService`` is the only class a caller touches.
"""

from __future__ import annotations

import heapq
import itertools
import threading
from collections.abc import Mapping

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.notification_service.channels import (
    ChannelSender,
    ExponentialBackoff,
    NullSender,
    RetryPolicy,
)
from lld.notification_service.models import (
    Channel,
    Notification,
    NotificationRequest,
    NotifyResult,
    Priority,
    QueueFullError,
    UserPreferences,
)
from lld.notification_service.pipeline import (
    DedupStage,
    DedupStore,
    EnqueueStage,
    Pipeline,
    PipelineContext,
    PreferenceStage,
    RateLimitStage,
    Stage,
    TemplateEngine,
    TemplateStage,
    TokenBucketRateLimiter,
)


# --8<-- [start:queue]
class NotificationQueue:
    """Bounded, due-time ordered, priority-broken ties. The backpressure boundary.

    ``_lock`` guards the heap and the counter. ``put`` refuses past capacity
    rather than growing without limit, because an unbounded queue turns a
    provider outage into an out-of-memory kill.
    """

    def __init__(self, capacity: int = 1000) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._heap: list[tuple[float, int, int, Notification]] = []
        self._sequence = itertools.count()

    def put(self, notification: Notification) -> None:
        with self._lock:
            if len(self._heap) >= self._capacity:
                raise QueueFullError(f"queue is full at {self._capacity}; shed load or scale workers")
            entry = (
                notification.due_at,
                -notification.priority.rank,
                next(self._sequence),
                notification,
            )
            heapq.heappush(self._heap, entry)

    def take(self, now: float) -> Notification | None:
        """Pop the earliest due notification, or None when nothing is ready yet."""
        with self._lock:
            if not self._heap or self._heap[0][0] > now:
                return None
            return heapq.heappop(self._heap)[3]

    def __len__(self) -> int:
        with self._lock:
            return len(self._heap)


class DeliveryLedger:
    """``(idempotency key, channel)`` may be handed to a provider at most once.

    The queue already gives one notification to one worker; this is the second
    line of defence, for the case a message is redelivered after a crash. It is
    what turns at-least-once transport into effectively-once delivery.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: set[tuple[str, Channel]] = set()

    def claim(self, key: str, channel: Channel) -> bool:
        with self._lock:
            if (key, channel) in self._claims:
                return False
            self._claims.add((key, channel))
            return True

    def release(self, key: str, channel: Channel) -> None:
        """A failed attempt gives the claim back so the retry can proceed."""
        with self._lock:
            self._claims.discard((key, channel))

    def delivered(self) -> int:
        with self._lock:
            return len(self._claims)


# --8<-- [end:queue]


# --8<-- [start:dispatcher]
class Dispatcher:
    """One worker step: claim, re-check preferences, send, retry, fall back, dead-letter."""

    def __init__(
        self,
        queue: NotificationQueue,
        senders: Mapping[Channel, ChannelSender],
        engine: TemplateEngine,
        preferences: PreferenceStore,
        ledger: DeliveryLedger,
        retry: RetryPolicy | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._queue = queue
        self._senders = dict(senders)
        self._engine = engine
        self._preferences = preferences
        self._ledger = ledger
        self._retry = retry or ExponentialBackoff()
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._dead_letters: list[Notification] = []

    def sender_for(self, channel: Channel) -> ChannelSender:
        """Null Object: an unconfigured channel is a no-op, never an exception."""
        return self._senders.get(channel) or NullSender(channel)

    def run_once(self) -> Notification | None:
        """Deliver one notification, or return None when nothing is due."""
        notification = self._queue.take(self._clock.now())
        if notification is None:
            return None
        if not self._still_wanted(notification):
            return notification
        notification.claim()
        key = notification.message.idempotency_key
        if not self._ledger.claim(key, notification.channel):
            # Someone already sent this exact message on this channel. Do not send it twice.
            notification.succeed("deduplicated", self._clock.now())
            return notification
        try:
            provider_message_id = self.sender_for(notification.channel).send(notification.message)
        except Exception as exc:  # any provider failure, including an open circuit
            self._ledger.release(key, notification.channel)
            notification.fail(str(exc), self._clock.now())
            self._recover(notification)
        else:
            notification.succeed(provider_message_id, self._clock.now())
        return notification

    def drain(self, limit: int = 10_000) -> list[Notification]:
        """Run until the queue has nothing due. Deterministic; used by the demo and tests."""
        done: list[Notification] = []
        for _ in range(limit):
            notification = self.run_once()
            if notification is None:
                return done
            done.append(notification)
        return done

    def dead_letters(self) -> list[Notification]:
        with self._lock:
            return list(self._dead_letters)

    def _still_wanted(self, notification: Notification) -> bool:
        """Preferences can change while a notification sits in the queue.

        Checking at send time rather than at enqueue time is the difference
        between honouring an opt-out and emailing someone who just left.
        """
        preferences = self._preferences.get(notification.user_id)
        if preferences.allows(notification.event, notification.channel):
            return True
        for channel in notification.fallbacks:
            if preferences.allows(notification.event, channel) and self._engine.has(
                notification.event, channel
            ):
                self._switch(notification, channel, preferences)
                return False
        notification.suppress()
        return False

    def _recover(self, notification: Notification) -> None:
        """Retry the same channel, then fall back, then dead-letter."""
        if notification.attempts < self._retry.max_attempts:
            notification.schedule_retry(self._clock.now() + self._retry.delay(notification.attempts))
            self._queue.put(notification)
            return
        preferences = self._preferences.get(notification.user_id)
        for channel in notification.fallbacks:
            if preferences.allows(notification.event, channel) and self._engine.has(
                notification.event, channel
            ):
                self._switch(notification, channel, preferences)
                return
        notification.dead_letter()
        with self._lock:
            self._dead_letters.append(notification)

    def _switch(self, notification: Notification, channel: Channel, preferences: UserPreferences) -> None:
        """Re-render for the new channel: an SMS body is not an email body."""
        message = self._engine.render(
            self._engine.template_for(notification.event, channel),
            notification.id,
            preferences.addresses[channel],
            notification.payload,
            notification.message.idempotency_key,
        )
        notification.switch_to(channel, message, self._clock.now())
        self._queue.put(notification)


# --8<-- [end:dispatcher]


# --8<-- [start:facade]
class PreferenceStore:
    """Preferences by user, with a permissive default so a new user still gets mail."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._preferences: dict[str, UserPreferences] = {}

    def set(self, preferences: UserPreferences) -> UserPreferences:
        with self._lock:
            self._preferences[preferences.user_id] = preferences
        return preferences

    def get(self, user_id: str) -> UserPreferences:
        with self._lock:
            return self._preferences.get(user_id) or UserPreferences(user_id)


class NotificationService:
    """The facade: build the request, run the pipeline, answer what happened."""

    def __init__(
        self,
        engine: TemplateEngine,
        preferences: PreferenceStore,
        queue: NotificationQueue,
        dedup: DedupStore,
        limiter: TokenBucketRateLimiter,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        stages: list[Stage] | None = None,
        request_ids: IdGenerator | None = None,
    ) -> None:
        self._engine = engine
        self._preferences = preferences
        self._queue = queue
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("n")
        self._request_ids = request_ids or SequentialIdGenerator("q")
        self._pipeline = Pipeline(
            stages
            or [
                PreferenceStage(),
                DedupStage(dedup),
                RateLimitStage(limiter),
                TemplateStage(engine),
                EnqueueStage(queue, self._clock, self._ids),
            ]
        )

    def stages(self) -> list[str]:
        return self._pipeline.names()

    def notify(
        self,
        user_id: str,
        event: str,
        payload: dict[str, str] | None = None,
        priority: Priority = Priority.NORMAL,
        idempotency_key: str = "",
        send_after: float = 0.0,
        channels: tuple[Channel, ...] = (),
    ) -> NotifyResult:
        request = NotificationRequest(
            id=self._request_ids.next_id(),
            user_id=user_id,
            event=event,
            payload=dict(payload or {}),
            priority=priority,
            idempotency_key=idempotency_key,
            requested_at=self._clock.now(),
            send_after=send_after,
            channels=channels,
        )
        ctx = PipelineContext(
            request=request,
            preferences=self._preferences.get(user_id),
            notification_id=self._ids.next_id(),
        )
        self._pipeline.run(ctx)
        return NotifyResult(request, ctx.notification, tuple(ctx.suppressions))

    def pending(self) -> int:
        return len(self._queue)


def status_counts(notifications: list[Notification]) -> dict[str, int]:
    """Small helper the demo uses to summarise a drain, sorted for stable output."""
    counts: dict[str, int] = {}
    for notification in notifications:
        counts[notification.status.value] = counts.get(notification.status.value, 0) + 1
    return dict(sorted(counts.items()))


# --8<-- [end:facade]
