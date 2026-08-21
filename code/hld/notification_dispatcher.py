"""Multi-channel notification dispatch: one queue per channel, plus every filter that keeps
users from muting you.

What the module demonstrates, in the order an interviewer asks about it:

* A ``Channel`` **protocol** every transport implements, so the dispatcher knows nothing about
  APNs, Twilio or SES beyond "send this and tell me whether the address is dead".
* The **admission chain**: idempotency (dedup key inside a TTL window), preferences, quiet
  hours, then the per-user rate limit. Every rejection is a distinct ``Outcome``, so the reason
  a notification never arrived is a counter rather than a mystery.
* **Reliability**: one queue per channel, exponential backoff with jitter, a dead-letter queue
  after the last attempt, and a circuit breaker per provider.
* **Device-token lifecycle**: a token the provider reports as unregistered is deleted, and a
  user with no live tokens is a ``NO_DEVICE`` outcome, never an endless retry.

Clock and jitter are injected, so the demo and the tests run in microseconds without sleeping.
Reuses ``hld.rate_limiters.TokenBucket``, ``hld.retry.BackoffPolicy`` and
``hld.circuit_breaker.CircuitBreaker`` rather than re-implementing any of them.
"""

from __future__ import annotations

import heapq
import itertools
import random
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Protocol

from common import Clock, SystemClock, ValidationError
from hld.circuit_breaker import BreakerPolicy, CircuitBreaker, CircuitOpenError
from hld.rate_limiters import TokenBucket
from hld.retry import BackoffPolicy, Jitter

SECONDS_PER_HOUR = 3_600
HOURS_PER_DAY = 24


# --8<-- [start:model]
class ChannelName(StrEnum):
    PUSH = "push"
    SMS = "sms"
    EMAIL = "email"


class Priority(IntEnum):
    MARKETING = 0
    NORMAL = 1
    CRITICAL = 2  # the only level that overrides quiet hours and the per-user rate limit


class Outcome(StrEnum):
    QUEUED = "queued"  # accepted, waiting for a worker
    SENT = "sent"
    RETRYING = "retrying"  # a transient failure; re-queued after a backoff
    DUPLICATE = "duplicate"  # same dedup key inside the window
    OPTED_OUT = "opted_out"  # the user turned this channel off
    QUIET_HOURS = "quiet_hours"  # accepted but deferred to the end of the window
    RATE_LIMITED = "rate_limited"
    NO_DEVICE = "no_device"  # every token for this user was unregistered
    DEAD_LETTERED = "dead_lettered"  # retries exhausted, or the provider circuit was open


@dataclass(frozen=True, slots=True)
class Notification:
    id: str
    user_id: str
    channel: ChannelName
    template: str
    dedup_key: str
    priority: Priority = Priority.NORMAL
    payload: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Preferences:
    """What the user agreed to. ``quiet_hours`` is a half-open [start, end) local-hour range."""

    enabled: frozenset[ChannelName] = frozenset(ChannelName)
    quiet_hours: tuple[int, int] | None = None
    utc_offset_hours: int = 0

    def allows(self, channel: ChannelName) -> bool:
        return channel in self.enabled

    def local_hour(self, epoch_seconds: float) -> int:
        return int(epoch_seconds // SECONDS_PER_HOUR + self.utc_offset_hours) % HOURS_PER_DAY

    def in_quiet_hours(self, epoch_seconds: float) -> bool:
        if self.quiet_hours is None:
            return False
        start, end = self.quiet_hours
        hour = self.local_hour(epoch_seconds)
        return start <= hour < end if start < end else hour >= start or hour < end

    def next_open_hour(self, epoch_seconds: float) -> float:
        """When quiet hours end, so a deferral is a timestamp rather than a guess."""
        if self.quiet_hours is None:
            return epoch_seconds
        _, end = self.quiet_hours
        ahead = (end - self.local_hour(epoch_seconds)) % HOURS_PER_DAY or HOURS_PER_DAY
        hour_start = epoch_seconds - epoch_seconds % SECONDS_PER_HOUR
        return hour_start + ahead * SECONDS_PER_HOUR


@dataclass(frozen=True, slots=True)
class Delivery:
    notification_id: str
    channel: ChannelName
    outcome: Outcome
    attempts: int
    provider_id: str = ""
    detail: str = ""


class UnregisteredAddress(Exception):
    """The provider says this token or address is permanently gone. Never retry it."""


class Channel(Protocol):
    """One transport; ``send`` returns a provider message id or raises."""

    name: ChannelName

    def send(self, notification: Notification, address: str) -> str: ...


# --8<-- [end:model]


# --8<-- [start:registry]
class DeviceRegistry:
    """Addresses per (user, channel); ``_lock`` guards ``_addresses``. Push tokens rot -- users
    reinstall, change device, revoke permission -- and APNs and FCM answer a send to a dead
    token with a permanent error. A registry that never prunes spends most of its push budget
    on devices that no longer exist."""

    def __init__(self) -> None:
        self._addresses: dict[tuple[str, ChannelName], list[str]] = {}
        self._lock = threading.Lock()

    def register(self, user_id: str, channel: ChannelName, address: str) -> None:
        if not address:
            raise ValidationError("address must be non-empty")
        with self._lock:
            addresses = self._addresses.setdefault((user_id, channel), [])
            if address not in addresses:
                addresses.append(address)

    def unregister(self, user_id: str, channel: ChannelName, address: str) -> None:
        with self._lock:
            addresses = self._addresses.get((user_id, channel), [])
            if address in addresses:
                addresses.remove(address)

    def addresses(self, user_id: str, channel: ChannelName) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._addresses.get((user_id, channel), ()))


# --8<-- [end:registry]


# --8<-- [start:dispatcher]
@dataclass(slots=True)
class _Pending:
    notification: Notification
    attempt: int
    ready_at: float


class NotificationDispatcher:
    """Admission chain, one queue per channel, retries with backoff, DLQ. ``_lock`` guards every
    mutable field: the per-channel heaps, the dedup table and the DLQ. Providers are called
    outside the lock, through their circuit breaker."""

    def __init__(
        self,
        channels: Iterable[Channel],
        registry: DeviceRegistry,
        backoff: BackoffPolicy | None = None,
        dedup_ttl: float = 300.0,
        rate_per_hour: float = 10.0,
        burst: int = 3,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._rng = rng or random.Random()
        self._backoff = backoff or BackoffPolicy(max_attempts=3, jitter=Jitter.EQUAL)
        self._dedup_ttl = dedup_ttl
        self._channels = {channel.name: channel for channel in channels}
        if not self._channels:
            raise ValidationError("at least one channel is required")
        self._registry = registry
        self._limiter = TokenBucket(rate_per_hour / SECONDS_PER_HOUR, burst, clock=self._clock)
        # A dead token is a user fact, not a provider fault, so it never trips the breaker.
        policy = BreakerPolicy(min_calls=3, ignored_exceptions=(UnregisteredAddress,))
        self._breakers = {n: CircuitBreaker(n.value, policy, self._clock) for n in self._channels}
        self._preferences: dict[str, Preferences] = {}
        self._queues: dict[ChannelName, list[tuple[float, int, _Pending]]] = {
            name: [] for name in self._channels
        }
        self._seen: dict[tuple[str, str], float] = {}
        self._dead_letters: list[Delivery] = []
        self._sequence = itertools.count()
        self._lock = threading.RLock()

    def set_preferences(self, user_id: str, preferences: Preferences) -> None:
        with self._lock:
            self._preferences[user_id] = preferences

    def preferences(self, user_id: str) -> Preferences:
        return self._preferences.get(user_id, Preferences())

    def dead_letters(self) -> tuple[Delivery, ...]:
        with self._lock:
            return tuple(self._dead_letters)

    def pending(self) -> int:
        with self._lock:
            return sum(len(queue) for queue in self._queues.values())

    def submit(self, notification: Notification) -> Delivery:
        """Run the admission chain and report the decision. Only ``QUEUED`` and ``QUIET_HOURS``
        are accepted; everything else is a drop with a reason a dashboard can count."""
        if notification.channel not in self._channels:
            raise ValidationError(f"no channel registered for {notification.channel}")
        preferences = self.preferences(notification.user_id)
        with self._lock:
            now = self._clock.now()
            self._expire_dedup(now)
            key = (notification.user_id, notification.dedup_key)
            if key in self._seen:
                return Delivery(notification.id, notification.channel, Outcome.DUPLICATE, 0)
            if not preferences.allows(notification.channel):
                return Delivery(notification.id, notification.channel, Outcome.OPTED_OUT, 0)
            ready_at = now
            deferred = False
            if notification.priority is not Priority.CRITICAL and preferences.in_quiet_hours(now):
                ready_at = preferences.next_open_hour(now)
                deferred = True
            if notification.priority is not Priority.CRITICAL:
                verdict = self._limiter.allow(f"{notification.user_id}:{notification.channel}")
                if not verdict.allowed:
                    detail = f"retry_after={verdict.retry_after:.0f}s"
                    return Delivery(notification.id, notification.channel, Outcome.RATE_LIMITED, 0, detail=detail)
            self._seen[key] = now
            self._push(notification, attempt=1, ready_at=ready_at)
        outcome = Outcome.QUIET_HOURS if deferred else Outcome.QUEUED
        return Delivery(notification.id, notification.channel, outcome, 0)

    def dispatch(self, limit: int = 100) -> list[Delivery]:
        """Drain everything that is due across every channel queue. One worker turn."""
        results: list[Delivery] = []
        for _ in range(limit):
            pending = self._take_due()
            if pending is None:
                break
            results.append(self._deliver(pending))
        return results

    def _take_due(self) -> _Pending | None:
        with self._lock:
            now = self._clock.now()
            due = [n for n, q in self._queues.items() if q and q[0][0] <= now]
            if not due:
                return None
            name = min(due, key=lambda n: self._queues[n][0][0])
            return heapq.heappop(self._queues[name])[2]

    def _deliver(self, pending: _Pending) -> Delivery:
        item = pending.notification
        channel = self._channels[item.channel]
        addresses = self._registry.addresses(item.user_id, item.channel)
        if not addresses:
            return Delivery(item.id, item.channel, Outcome.NO_DEVICE, pending.attempt)
        try:
            provider_id = self._breakers[item.channel].call(channel.send, item, addresses[0])
        except UnregisteredAddress:
            self._registry.unregister(item.user_id, item.channel, addresses[0])
            return self._retry_or_dead(pending, "address unregistered", immediate=True)
        except CircuitOpenError as exc:
            return self._dead_letter(pending, f"provider circuit open: {exc.retry_after:.0f}s")
        except Exception as exc:
            return self._retry_or_dead(pending, f"{type(exc).__name__}: {exc}")
        return Delivery(item.id, item.channel, Outcome.SENT, pending.attempt, provider_id)

    def _retry_or_dead(self, pending: _Pending, detail: str, immediate: bool = False) -> Delivery:
        if pending.attempt >= self._backoff.max_attempts:
            return self._dead_letter(pending, detail)
        with self._lock:
            delay = 0.0 if immediate else self._jittered_delay(pending.attempt)
            self._push(pending.notification, pending.attempt + 1, self._clock.now() + delay)
        item = pending.notification
        return Delivery(item.id, item.channel, Outcome.RETRYING, pending.attempt, detail=detail)

    def _jittered_delay(self, attempt: int) -> float:
        """Equal jitter: half the exponential delay plus a random half, so retries spread out."""
        exponential = self._backoff.exponential(attempt)
        return exponential / 2 + self._rng.uniform(0.0, exponential / 2)

    def _dead_letter(self, pending: _Pending, detail: str) -> Delivery:
        item = pending.notification
        delivery = Delivery(
            item.id, item.channel, Outcome.DEAD_LETTERED, pending.attempt, detail=detail
        )
        with self._lock:
            self._dead_letters.append(delivery)
        return delivery

    def _push(self, notification: Notification, attempt: int, ready_at: float) -> None:
        entry = (ready_at, next(self._sequence), _Pending(notification, attempt, ready_at))
        heapq.heappush(self._queues[notification.channel], entry)

    def _expire_dedup(self, now: float) -> None:
        stale = [key for key, seen_at in self._seen.items() if now - seen_at > self._dedup_ttl]
        for key in stale:
            del self._seen[key]


# --8<-- [end:dispatcher]


# --8<-- [start:channels]
class RecordingChannel:
    """A provider double (APNs, FCM, Twilio, SES): the first ``failures`` attempts fail, and
    ``dead_addresses`` are tokens the provider calls permanently unregistered."""

    def __init__(self, name: ChannelName, failures: int = 0, dead: Sequence[str] = ()) -> None:
        self.name = name
        self.sent: list[tuple[str, str]] = []
        self._remaining_failures = failures
        self._dead = set(dead)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def send(self, notification: Notification, address: str) -> str:
        with self._lock:
            if address in self._dead:
                raise UnregisteredAddress(address)
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                raise TimeoutError(f"{self.name} provider timed out")
            self.sent.append((notification.id, address))
            return f"{self.name}-{next(self._ids)}"


# --8<-- [end:channels]


def main() -> None:
    from common import FakeClock

    midnight = 1_700_000_000 - 1_700_000_000 % 86_400
    clock = FakeClock(start=midnight + 22 * SECONDS_PER_HOUR)  # 22:00, inside quiet hours
    registry = DeviceRegistry()
    registry.register("u1", ChannelName.PUSH, "apns-token-a")
    registry.register("u1", ChannelName.EMAIL, "u1@example.com")
    registry.register("u2", ChannelName.PUSH, "apns-token-dead")
    push = RecordingChannel(ChannelName.PUSH, failures=1, dead=("apns-token-dead",))
    email = RecordingChannel(ChannelName.EMAIL)
    dispatcher = NotificationDispatcher(
        [push, email],
        registry,
        backoff=BackoffPolicy(base_seconds=1.0, max_attempts=2, jitter=Jitter.EQUAL),
        rate_per_hour=2.0,
        burst=1,
        clock=clock,
        rng=random.Random(42),
    )
    dispatcher.set_preferences("u1", Preferences(quiet_hours=(22, 7)))
    dispatcher.set_preferences("u2", Preferences(enabled=frozenset({ChannelName.PUSH})))

    submissions = [
        Notification("n1", "u1", ChannelName.PUSH, "like", "like:post9", Priority.NORMAL),
        Notification("n2", "u1", ChannelName.PUSH, "like", "like:post9", Priority.NORMAL),
        Notification("n3", "u1", ChannelName.PUSH, "reply", "reply:post9", Priority.NORMAL),
        Notification("n4", "u1", ChannelName.PUSH, "fraud", "fraud:1", Priority.CRITICAL),
        Notification("n5", "u1", ChannelName.EMAIL, "digest", "digest:1", Priority.MARKETING),
        Notification("n6", "u2", ChannelName.EMAIL, "receipt", "receipt:1", Priority.NORMAL),
        Notification("n7", "u2", ChannelName.PUSH, "promo", "promo:1", Priority.MARKETING),
    ]
    for notification in submissions:
        decision = dispatcher.submit(notification)
        print(f"submit {notification.id} {notification.channel.value:5s} p{int(notification.priority)} -> {decision.outcome.value}")

    print(f"pending={dispatcher.pending()}")
    for label, advance in (("now    ", 0.0), ("backoff", 2.0), ("morning", 9 * SECONDS_PER_HOUR)):
        clock.advance(advance)
        for done in dispatcher.dispatch():
            print(f"  {label} {done.notification_id} #{done.attempts} -> {done.outcome.value} {done.detail}".rstrip())
    print(f"push sent={[n for n, _ in push.sent]} dead_letters={[d.notification_id for d in dispatcher.dead_letters()]}")
    print(f"u2 push tokens after the unregistered error: {registry.addresses('u2', ChannelName.PUSH)}")


if __name__ == "__main__":
    main()
