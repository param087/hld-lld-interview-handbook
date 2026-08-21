"""Channel senders, the retry decorator, and one circuit breaker per provider.

``ChannelSender`` is a ``Protocol``, so a provider stub is any object with a
``channel`` and a ``send``. The two decorators wrap *any* sender, which is why
retry and provider isolation are configured, not coded, per channel.
"""

from __future__ import annotations

import random
import threading
from typing import Protocol

from common import Clock
from lld.notification_service.models import (
    Channel,
    ChannelUnavailableError,
    CircuitState,
    RenderedMessage,
)


# --8<-- [start:senders]
class ChannelSender(Protocol):
    """One provider. Returns the provider's message id, or raises to signal failure."""

    channel: Channel

    def send(self, message: RenderedMessage) -> str: ...


class RecordingSender:
    """The stub every channel uses in this package: it records and returns an id.

    Real senders differ only in what they do with ``message``; the pipeline,
    the retry decorator and the breaker never learn which one they hold.
    """

    def __init__(self, channel: Channel, prefix: str) -> None:
        self.channel = channel
        self._prefix = prefix
        self._lock = threading.Lock()
        self.sent: list[RenderedMessage] = []

    def send(self, message: RenderedMessage) -> str:
        with self._lock:
            self.sent.append(message)
            return f"{self._prefix}-{len(self.sent)}"

    def count(self) -> int:
        with self._lock:
            return len(self.sent)


class NullSender:
    """Null Object: a channel with no provider configured.

    It accepts and discards, so the dispatcher never needs
    ``if sender is None`` and a missing provider degrades to a no-op instead of
    an AttributeError at three in the morning.
    """

    def __init__(self, channel: Channel) -> None:
        self.channel = channel

    def send(self, message: RenderedMessage) -> str:
        return "null"


class FlakySender:
    """Fails its first ``failures`` calls, then succeeds. Deterministic by design."""

    def __init__(self, channel: Channel, failures: int, error: str = "provider timeout") -> None:
        self.channel = channel
        self._remaining = failures
        self._error = error
        self._lock = threading.Lock()
        self.calls = 0

    def send(self, message: RenderedMessage) -> str:
        with self._lock:
            self.calls += 1
            if self._remaining > 0:
                self._remaining -= 1
                raise ChannelUnavailableError(f"{self.channel}: {self._error}")
            return f"{self.channel}-{self.calls}"


# --8<-- [end:senders]


# --8<-- [start:retry]
class RetryPolicy(Protocol):
    max_attempts: int

    def delay(self, attempt: int) -> float:
        """Seconds to wait before attempt number ``attempt`` (1-based)."""
        ...


class ExponentialBackoff:
    """``base * factor^(attempt-1)`` with bounded jitter, capped.

    The jitter source is injected so a test asserts exact delays; in production
    it stops every retry from every worker landing on the provider at once.
    """

    def __init__(
        self,
        base: float = 1.0,
        factor: float = 2.0,
        max_attempts: int = 3,
        cap: float = 60.0,
        jitter: random.Random | None = None,
    ) -> None:
        self.base = base
        self.factor = factor
        self.max_attempts = max_attempts
        self.cap = cap
        self._jitter = jitter or random.Random(42)

    def delay(self, attempt: int) -> float:
        raw = min(self.cap, self.base * self.factor ** max(0, attempt - 1))
        return round(raw * (0.5 + self._jitter.random() / 2), 3)


class NoRetry:
    """One shot. Useful for `in_app`, where a failure means the row did not write."""

    max_attempts = 1

    def delay(self, attempt: int) -> float:
        return 0.0


# --8<-- [end:retry]


# --8<-- [start:breaker]
class CircuitBreaker:
    """One breaker per provider. CLOSED -> OPEN -> HALF_OPEN -> CLOSED.

    ``_lock`` guards the failure counter and the state. The point is isolation:
    an SMS provider that starts timing out must not hold up email, and once it
    is known bad every call fails in nanoseconds instead of waiting on a socket.
    """

    def __init__(self, clock: Clock, threshold: int = 3, cooldown: float = 30.0) -> None:
        self._clock = clock
        self._threshold = threshold
        self._cooldown = cooldown
        self._lock = threading.Lock()
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._refresh()

    def allow(self) -> bool:
        with self._lock:
            return self._refresh() is not CircuitState.OPEN

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            if self._refresh() is CircuitState.HALF_OPEN:
                self._trip()  # the probe failed: straight back to OPEN
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock.now()

    def _refresh(self) -> CircuitState:
        """Caller holds the lock. Moves OPEN to HALF_OPEN once the cooldown elapses."""
        if self._state is CircuitState.OPEN and self._clock.now() - self._opened_at >= self._cooldown:
            self._state = CircuitState.HALF_OPEN
        return self._state


class CircuitBreakerSender:
    """Decorator: fails fast while the provider is known bad."""

    def __init__(self, inner: ChannelSender, breaker: CircuitBreaker) -> None:
        self.channel = inner.channel
        self._inner = inner
        self._breaker = breaker

    def send(self, message: RenderedMessage) -> str:
        if not self._breaker.allow():
            raise ChannelUnavailableError(f"{self.channel}: circuit open, not calling the provider")
        try:
            provider_id = self._inner.send(message)
        except Exception:
            self._breaker.record_failure()
            raise
        self._breaker.record_success()
        return provider_id


# --8<-- [end:breaker]
