import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.notification_dispatcher import (
    SECONDS_PER_HOUR,
    ChannelName,
    DeviceRegistry,
    Notification,
    NotificationDispatcher,
    Outcome,
    Preferences,
    Priority,
    RecordingChannel,
)
from hld.retry import BackoffPolicy, Jitter

MIDNIGHT = 1_700_000_000 - 1_700_000_000 % 86_400


def at_hour(hour: int) -> FakeClock:
    return FakeClock(start=MIDNIGHT + hour * SECONDS_PER_HOUR)


def build(
    clock: FakeClock,
    failures: int = 0,
    dead: tuple[str, ...] = (),
    max_attempts: int = 3,
    rate_per_hour: float = 100.0,
    burst: int = 10,
) -> tuple[NotificationDispatcher, RecordingChannel, DeviceRegistry]:
    registry = DeviceRegistry()
    registry.register("u1", ChannelName.PUSH, "token-a")
    push = RecordingChannel(ChannelName.PUSH, failures=failures, dead=dead)
    dispatcher = NotificationDispatcher(
        [push],
        registry,
        backoff=BackoffPolicy(base_seconds=1.0, max_attempts=max_attempts, jitter=Jitter.EQUAL),
        rate_per_hour=rate_per_hour,
        burst=burst,
        clock=clock,
        rng=random.Random(42),
    )
    return dispatcher, push, registry


def push_note(note_id: str, dedup: str, priority: Priority = Priority.NORMAL) -> Notification:
    return Notification(note_id, "u1", ChannelName.PUSH, "tpl", dedup, priority)


def test_happy_path_sends_once_and_records_the_provider_id() -> None:
    dispatcher, push, _ = build(at_hour(12))
    assert dispatcher.submit(push_note("n1", "d1")).outcome is Outcome.QUEUED
    (delivery,) = dispatcher.dispatch()
    assert delivery.outcome is Outcome.SENT
    assert delivery.attempts == 1
    assert delivery.provider_id.startswith("push-")
    assert push.sent == [("n1", "token-a")]


def test_the_dedup_key_makes_submit_idempotent() -> None:
    clock = at_hour(12)
    dispatcher, push, _ = build(clock)
    dispatcher.submit(push_note("n1", "like:post9"))
    assert dispatcher.submit(push_note("n2", "like:post9")).outcome is Outcome.DUPLICATE
    clock.advance(400.0)  # past the 300 s dedup TTL
    assert dispatcher.submit(push_note("n3", "like:post9")).outcome is Outcome.QUEUED
    assert {d.outcome for d in dispatcher.dispatch()} == {Outcome.SENT}
    assert [n for n, _ in push.sent] == ["n1", "n3"]


@pytest.mark.parametrize(
    "hour, priority, expected",
    [
        (23, Priority.NORMAL, Outcome.QUIET_HOURS),
        (23, Priority.CRITICAL, Outcome.QUEUED),
        (12, Priority.NORMAL, Outcome.QUEUED),
    ],
)
def test_quiet_hours_defer_everything_except_critical(
    hour: int, priority: Priority, expected: Outcome
) -> None:
    clock = at_hour(hour)
    dispatcher, _, _ = build(clock)
    dispatcher.set_preferences("u1", Preferences(quiet_hours=(22, 7)))
    assert dispatcher.submit(push_note("n1", "d1", priority)).outcome is expected


def test_a_deferred_notification_is_delivered_when_quiet_hours_end() -> None:
    clock = at_hour(23)
    dispatcher, push, _ = build(clock)
    dispatcher.set_preferences("u1", Preferences(quiet_hours=(22, 7)))
    dispatcher.submit(push_note("n1", "d1"))
    assert dispatcher.dispatch() == []  # nothing is due yet
    assert dispatcher.pending() == 1
    clock.advance(8 * SECONDS_PER_HOUR)  # 07:00
    (delivery,) = dispatcher.dispatch()
    assert delivery.outcome is Outcome.SENT
    assert push.sent == [("n1", "token-a")]


def test_preferences_and_rate_limits_drop_with_distinct_reasons() -> None:
    dispatcher, _, _ = build(at_hour(12), rate_per_hour=1.0, burst=1)
    assert dispatcher.submit(push_note("n1", "d1")).outcome is Outcome.QUEUED
    limited = dispatcher.submit(push_note("n2", "d2"))
    assert limited.outcome is Outcome.RATE_LIMITED
    assert "retry_after" in limited.detail
    assert dispatcher.submit(push_note("n3", "d3", Priority.CRITICAL)).outcome is Outcome.QUEUED
    dispatcher.set_preferences("u1", Preferences(enabled=frozenset({ChannelName.EMAIL})))
    assert dispatcher.submit(push_note("n4", "d4")).outcome is Outcome.OPTED_OUT


def test_transient_failures_retry_with_backoff_then_dead_letter() -> None:
    clock = at_hour(12)
    dispatcher, _, _ = build(clock, failures=5, max_attempts=3)
    dispatcher.submit(push_note("n1", "d1"))
    outcomes = []
    for _ in range(4):
        outcomes += [d.outcome for d in dispatcher.dispatch()]
        clock.advance(10.0)  # longer than any capped backoff
    assert outcomes == [Outcome.RETRYING, Outcome.RETRYING, Outcome.DEAD_LETTERED]
    assert [d.notification_id for d in dispatcher.dead_letters()] == ["n1"]
    assert dispatcher.pending() == 0


def test_an_unregistered_token_is_deleted_instead_of_retried_forever() -> None:
    dispatcher, push, registry = build(at_hour(12), dead=("token-a",))
    registry.register("u1", ChannelName.PUSH, "token-b")
    dispatcher.submit(push_note("n1", "d1"))
    outcomes = [d.outcome for d in dispatcher.dispatch()]
    assert outcomes == [Outcome.RETRYING, Outcome.SENT]  # falls through to the live token
    assert registry.addresses("u1", ChannelName.PUSH) == ("token-b",)
    assert push.sent == [("n1", "token-b")]


def test_a_user_with_no_live_addresses_is_not_a_retry() -> None:
    dispatcher, _, registry = build(at_hour(12))
    registry.unregister("u1", ChannelName.PUSH, "token-a")
    dispatcher.submit(push_note("n1", "d1"))
    (delivery,) = dispatcher.dispatch()
    assert delivery.outcome is Outcome.NO_DEVICE
    assert dispatcher.dead_letters() == ()


def test_unknown_channel_and_empty_configuration_are_rejected() -> None:
    dispatcher, _, _ = build(at_hour(12))
    with pytest.raises(ValidationError):
        dispatcher.submit(Notification("x", "u1", ChannelName.SMS, "tpl", "d"))
    with pytest.raises(ValidationError):
        NotificationDispatcher([], DeviceRegistry())
    with pytest.raises(ValidationError):
        DeviceRegistry().register("u1", ChannelName.PUSH, "")


def test_concurrent_submissions_of_one_dedup_key_queue_exactly_one() -> None:
    clock = at_hour(12)
    dispatcher, push, _ = build(clock, rate_per_hour=10_000.0, burst=500)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(lambda i: dispatcher.submit(push_note(f"n{i}", "same-key")).outcome, range(200))
        )
    assert outcomes.count(Outcome.QUEUED) == 1
    assert outcomes.count(Outcome.DUPLICATE) == 199
    assert dispatcher.pending() == 1
    assert [d.outcome for d in dispatcher.dispatch()] == [Outcome.SENT]
    assert len(push.sent) == 1
