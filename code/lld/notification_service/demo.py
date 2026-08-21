"""The whole pipeline in one run: dedup, rate limit, retry, fallback, breaker, DLQ."""

import random

from common import FakeClock, SequentialIdGenerator
from lld.notification_service.channels import (
    CircuitBreaker,
    CircuitBreakerSender,
    ExponentialBackoff,
    FlakySender,
    RecordingSender,
)
from lld.notification_service.models import (
    Channel,
    Notification,
    Priority,
    Template,
    UserPreferences,
)
from lld.notification_service.pipeline import DedupStore, TemplateEngine, TokenBucketRateLimiter
from lld.notification_service.services import (
    DeliveryLedger,
    Dispatcher,
    NotificationQueue,
    NotificationService,
    PreferenceStore,
    status_counts,
)

EVENT = "order_shipped"
TEMPLATES = [
    Template("t-push", EVENT, Channel.PUSH, "Shipped", "Order {order} is on the way"),
    Template("t-email", EVENT, Channel.EMAIL, "Order {order} shipped", "Tracking: {tracking}"),
    Template("t-sms", EVENT, Channel.SMS, "Shipped", "Order {order}: {tracking}"),
]


def pump(dispatcher: Dispatcher, clock: FakeClock, rounds: int, step: float = 30.0) -> list[Notification]:
    """Drain, let the backoff elapse, drain again. A real worker would just block."""
    processed: list[Notification] = []
    for _ in range(rounds):
        processed.extend(dispatcher.drain())
        clock.advance(step)
    return processed


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    engine = TemplateEngine()
    for template in TEMPLATES:
        engine.register(template)
    preferences, queue, ledger = PreferenceStore(), NotificationQueue(capacity=50), DeliveryLedger()
    preferences.set(
        UserPreferences(
            "u-1",
            channel_order=(Channel.PUSH, Channel.EMAIL, Channel.SMS),
            addresses={Channel.PUSH: "device-1", Channel.EMAIL: "u1@example.com", Channel.SMS: "+100"},
        )
    )
    preferences.set(UserPreferences("u-2", (Channel.PUSH,), addresses={Channel.PUSH: "device-2"}))
    service = NotificationService(
        engine,
        preferences,
        queue,
        DedupStore(clock, window_seconds=300),
        TokenBucketRateLimiter(clock, capacity=2, refill_per_second=0.0),
        clock=clock,
        ids=SequentialIdGenerator("n"),
        request_ids=SequentialIdGenerator("q"),
    )
    print(f"pipeline: {' -> '.join(service.stages())}")

    push = FlakySender(Channel.PUSH, failures=99, error="APNs 503")
    breaker = CircuitBreaker(clock, threshold=2, cooldown=30.0)
    email = RecordingSender(Channel.EMAIL, "eml")
    dispatcher = Dispatcher(
        queue,
        {Channel.PUSH: CircuitBreakerSender(push, breaker), Channel.EMAIL: email},
        engine,
        preferences,
        ledger,
        retry=ExponentialBackoff(base=2.0, factor=2.0, max_attempts=2, jitter=random.Random(42)),
        clock=clock,
    )

    payload = {"order": "A-1", "tracking": "TRK9"}
    first = service.notify("u-1", EVENT, payload, idempotency_key="ship:A-1")
    again = service.notify("u-1", EVENT, payload, idempotency_key="ship:A-1")
    print(f"first: {first.line()} | repeat: {again.line()}")

    pump(dispatcher, clock, rounds=3, step=10)
    print(f"push calls={push.calls}, email sends={email.count()}, breaker={breaker.state}")
    print(f"delivery: {first.notification.summary()}")
    print(f"attempts: {[(a.attempt, str(a.channel), a.ok) for a in first.notification.history]}")

    second = service.notify("u-1", EVENT, {"order": "A-2", "tracking": "TRK8"}, idempotency_key="A-2")
    third = service.notify("u-1", EVENT, {"order": "A-3", "tracking": "TRK7"}, idempotency_key="A-3")
    print(f"rate limit, capacity 2: {second.line()} | {third.line()}")
    urgent = service.notify("u-1", EVENT, {"order": "A-4", "tracking": "TRK6"}, Priority.CRITICAL, "A-4")
    print(f"critical bypasses the limit: {urgent.line()}")

    preferences.set(preferences.get("u-1").muting(EVENT))  # opt out while A-2 and A-4 are queued
    print(f"after the opt-out: {status_counts(pump(dispatcher, clock, rounds=3))}")

    stranded = service.notify("u-2", EVENT, {"order": "B-1", "tracking": "TRK5"}, idempotency_key="B-1")
    pump(dispatcher, clock, rounds=4)
    print(f"u-2 has push only: {stranded.notification.summary()}")
    print(f"dead letters: {[n.id for n in dispatcher.dead_letters()]}")
    print(f"queue depth={len(queue)}, distinct deliveries={ledger.delivered()}")


if __name__ == "__main__":
    main()
