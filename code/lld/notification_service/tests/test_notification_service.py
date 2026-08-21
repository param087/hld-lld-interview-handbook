import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from common import FakeClock, SequentialIdGenerator
from lld.notification_service.channels import (
    CircuitBreaker,
    CircuitBreakerSender,
    ExponentialBackoff,
    FlakySender,
    NullSender,
    RecordingSender,
)
from lld.notification_service.models import (
    Channel,
    CircuitState,
    DeliveryStatus,
    Priority,
    QueueFullError,
    RenderedMessage,
    RenderError,
    SuppressionReason,
    Template,
    TemplateNotFoundError,
    UserPreferences,
)
from lld.notification_service.pipeline import DedupStore, TemplateEngine, TokenBucketRateLimiter
from lld.notification_service.services import (
    DeliveryLedger,
    Dispatcher,
    NotificationQueue,
    NotificationService,
    PreferenceStore,
)

EVENT = "order_shipped"
PAYLOAD = {"order": "A-1", "tracking": "TRK9"}


@dataclass(slots=True)
class Rig:
    """The whole stack wired up, so each test reads as a scenario."""

    clock: FakeClock
    service: NotificationService
    dispatcher: Dispatcher
    queue: NotificationQueue
    preferences: PreferenceStore
    ledger: DeliveryLedger
    push: RecordingSender
    email: RecordingSender
    engine: TemplateEngine

    def pump(self, rounds: int = 4, step: float = 30.0) -> list:
        processed: list = []
        for _ in range(rounds):
            processed.extend(self.dispatcher.drain())
            self.clock.advance(step)
        return processed


def build(
    push_sender: object | None = None,
    retry: object | None = None,
    capacity: int = 10,
    queue_capacity: int = 100,
) -> Rig:
    clock = FakeClock(start=1_000_000)
    engine = TemplateEngine()
    engine.register(Template("t-push", EVENT, Channel.PUSH, "Shipped", "Order {order} on the way"))
    engine.register(Template("t-email", EVENT, Channel.EMAIL, "Order {order}", "Tracking {tracking}"))
    preferences, queue, ledger = PreferenceStore(), NotificationQueue(queue_capacity), DeliveryLedger()
    preferences.set(
        UserPreferences(
            "u-1",
            (Channel.PUSH, Channel.EMAIL),
            addresses={Channel.PUSH: "device-1", Channel.EMAIL: "u1@example.com"},
        )
    )
    push, email = RecordingSender(Channel.PUSH, "psh"), RecordingSender(Channel.EMAIL, "eml")
    service = NotificationService(
        engine,
        preferences,
        queue,
        DedupStore(clock, window_seconds=300),
        TokenBucketRateLimiter(clock, capacity=capacity, refill_per_second=0.0),
        clock=clock,
        ids=SequentialIdGenerator("n"),
        request_ids=SequentialIdGenerator("q"),
    )
    dispatcher = Dispatcher(
        queue,
        {Channel.PUSH: push_sender or push, Channel.EMAIL: email},
        engine,
        preferences,
        ledger,
        retry=retry or ExponentialBackoff(base=1.0, max_attempts=2, jitter=random.Random(42)),
        clock=clock,
    )
    return Rig(clock, service, dispatcher, queue, preferences, ledger, push, email, engine)


def test_happy_path_renders_enqueues_and_delivers_on_the_preferred_channel() -> None:
    rig = build()
    result = rig.service.notify("u-1", EVENT, PAYLOAD, idempotency_key="A-1")
    assert result.accepted() and result.notification.channel is Channel.PUSH
    assert result.notification.status is DeliveryStatus.QUEUED and rig.service.pending() == 1

    rig.dispatcher.run_once()
    assert result.notification.status is DeliveryStatus.DELIVERED
    assert rig.push.count() == 1 and rig.email.count() == 0
    assert rig.push.sent[0].subject == "Shipped" and rig.push.sent[0].body == "Order A-1 on the way"
    assert rig.push.sent[0].recipient == "device-1" and rig.ledger.delivered() == 1


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("mute", SuppressionReason.OPTED_OUT),
        ("duplicate", SuppressionReason.DUPLICATE),
        ("rate_limit", SuppressionReason.RATE_LIMITED),
        ("no_address", SuppressionReason.NO_CHANNEL),
    ],
)
def test_each_pipeline_stage_drops_for_its_own_reason(setup: str, reason: SuppressionReason) -> None:
    rig = build(capacity=1)
    if setup == "mute":
        rig.preferences.set(rig.preferences.get("u-1").muting(EVENT))
    if setup == "no_address":
        rig.preferences.set(UserPreferences("u-1", (Channel.PUSH,)))
    if setup in ("duplicate", "rate_limit"):
        rig.service.notify("u-1", EVENT, PAYLOAD, idempotency_key="A-1")
    key = "A-1" if setup == "duplicate" else "A-2"

    result = rig.service.notify("u-1", EVENT, PAYLOAD, idempotency_key=key)
    assert not result.accepted()
    assert [s.reason for s in result.suppressions] == [reason]


def test_validation_rejects_a_payload_that_does_not_fill_the_template() -> None:
    rig = build()
    with pytest.raises(RenderError, match="tracking"):
        rig.service.notify("u-1", EVENT, {"order": "A-1"}, idempotency_key="A-1")
    with pytest.raises(TemplateNotFoundError):
        rig.engine.template_for("unknown_event", Channel.PUSH)
    assert rig.service.pending() == 0

    # An unconfigured channel is a Null Object, not a crash.
    assert isinstance(rig.dispatcher.sender_for(Channel.SMS), NullSender)
    assert rig.dispatcher.sender_for(Channel.SMS).send(
        RenderedMessage("n-0", Channel.SMS, "+1", "s", "b", "k")
    ) == "null"


# --8<-- [start:fallback]
def test_retries_then_falls_back_then_dead_letters() -> None:
    """Push fails forever: two attempts, then email; with no email, the dead letter queue."""
    rig = build(push_sender=FlakySender(Channel.PUSH, failures=99, error="APNs 503"))
    result = rig.service.notify("u-1", EVENT, PAYLOAD, idempotency_key="A-1")
    rig.pump()

    notification = result.notification
    assert notification.status is DeliveryStatus.DELIVERED and notification.channel is Channel.EMAIL
    assert [(a.attempt, a.channel, a.ok) for a in notification.history] == [
        (1, Channel.PUSH, False),
        (2, Channel.PUSH, False),
        (1, Channel.EMAIL, True),
    ]
    assert rig.email.count() == 1 and rig.dispatcher.dead_letters() == []

    lonely = build(push_sender=FlakySender(Channel.PUSH, failures=99))
    lonely.preferences.set(UserPreferences("u-2", (Channel.PUSH,), addresses={Channel.PUSH: "d-2"}))
    stranded = lonely.service.notify("u-2", EVENT, PAYLOAD, idempotency_key="B-1")
    lonely.pump()
    assert stranded.notification.status is DeliveryStatus.DEAD_LETTER
    assert [n.id for n in lonely.dispatcher.dead_letters()] == [stranded.notification.id]


# --8<-- [end:fallback]


def test_the_circuit_breaker_opens_then_half_opens_then_closes() -> None:
    clock = FakeClock(start=1_000_000)
    breaker = CircuitBreaker(clock, threshold=2, cooldown=30.0)
    flaky = FlakySender(Channel.PUSH, failures=2)
    sender = CircuitBreakerSender(flaky, breaker)
    message = RenderedMessage("n-1", Channel.PUSH, "device", "s", "b", "k")

    for _ in range(2):
        with pytest.raises(Exception, match="provider timeout"):
            sender.send(message)
    assert breaker.state is CircuitState.OPEN

    with pytest.raises(Exception, match="circuit open"):
        sender.send(message)
    assert flaky.calls == 2  # the provider was never called while the circuit was open

    clock.advance(30)
    assert breaker.state is CircuitState.HALF_OPEN
    assert sender.send(message).startswith("push")  # the probe succeeds
    assert breaker.state is CircuitState.CLOSED and flaky.calls == 3


def test_preferences_changed_while_queued_are_honoured_at_send_time() -> None:
    rig = build()
    result = rig.service.notify("u-1", EVENT, PAYLOAD, idempotency_key="A-1")
    assert result.notification.channel is Channel.PUSH

    # The member disables push while the notification sits in the queue.
    rig.preferences.set(
        UserPreferences(
            "u-1",
            (Channel.PUSH, Channel.EMAIL),
            disabled_channels=frozenset({Channel.PUSH}),
            addresses={Channel.PUSH: "device-1", Channel.EMAIL: "u1@example.com"},
        )
    )
    rig.pump()
    assert result.notification.channel is Channel.EMAIL and rig.push.count() == 0

    other = rig.service.notify("u-1", EVENT, PAYLOAD, idempotency_key="A-2")
    rig.preferences.set(rig.preferences.get("u-1").muting(EVENT))
    rig.pump()
    assert other.notification.status is DeliveryStatus.SUPPRESSED and rig.email.count() == 1


# --8<-- [start:concurrency]
def test_one_idempotency_key_under_concurrency_produces_exactly_one_send() -> None:
    """Twelve callers race with the same key; twelve workers race to deliver."""
    rig = build(capacity=100, queue_capacity=200)

    def submit(i: int) -> bool:
        return rig.service.notify("u-1", EVENT, PAYLOAD, idempotency_key="A-1").accepted()

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(submit, range(12)))
    assert accepted.count(True) == 1 and accepted.count(False) == 11

    # Now 30 distinct notifications, drained by four worker threads at once.
    for i in range(30):
        rig.service.notify("u-1", EVENT, PAYLOAD, idempotency_key=f"B-{i}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: rig.dispatcher.drain(), range(4)))

    assert rig.push.count() == 31  # 30 distinct plus the single deduplicated one
    assert rig.ledger.delivered() == 31 and len(rig.queue) == 0
    assert len({m.idempotency_key for m in rig.push.sent}) == 31  # nothing sent twice


# --8<-- [end:concurrency]


def test_priority_scheduling_and_a_bounded_queue() -> None:
    rig = build(capacity=100, queue_capacity=2)
    later = rig.service.notify(
        "u-1", EVENT, PAYLOAD, idempotency_key="A-1", send_after=rig.clock.now() + 60
    )
    urgent = rig.service.notify("u-1", EVENT, PAYLOAD, Priority.CRITICAL, "A-2")
    assert rig.dispatcher.run_once() is urgent.notification  # due now beats scheduled
    assert rig.dispatcher.run_once() is None  # the scheduled one is not due yet

    rig.clock.advance(60)
    assert rig.dispatcher.run_once() is later.notification

    for i in range(2):
        rig.service.notify("u-1", EVENT, PAYLOAD, idempotency_key=f"C-{i}")
    with pytest.raises(QueueFullError, match="queue is full at 2"):
        rig.service.notify("u-1", EVENT, PAYLOAD, idempotency_key="C-2")  # backpressure, not growth
    assert len(rig.queue) == 2


@pytest.mark.parametrize(("attempt", "expected"), [(1, 0.662), (2, 1.151), (3, 3.302)])
def test_backoff_is_exponential_and_deterministic(attempt: int, expected: float) -> None:
    policy = ExponentialBackoff(base=1.0, factor=2.0, max_attempts=4, jitter=random.Random(7))
    delays = [policy.delay(a) for a in range(1, 4)]
    assert delays[attempt - 1] == expected
    assert delays[2] > delays[0]  # later attempts wait longer
