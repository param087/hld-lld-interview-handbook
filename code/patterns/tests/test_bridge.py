"""Bridge: every abstraction works with every implementor, and either side grows alone."""

import json

import pytest

from common import ValidationError
from patterns.bridge import (
    SMS_MAX_CHARS,
    URGENT_MARKER,
    Alert,
    Channel,
    Delivery,
    DigestNotification,
    EmailChannel,
    NormalNotification,
    Notification,
    Priority,
    PushChannel,
    SmsChannel,
    UrgentNotification,
)

RECIPIENT = "user-42"
SUBJECT, BODY = "Disk almost full", "db-3 at 95%"
CHANNELS = [EmailChannel(), SmsChannel(), PushChannel()]


class RecordingChannel:
    """A fourth implementor written in a test: no base class, no change to any Notification."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, Priority]] = []

    def deliver(self, recipient: str, subject: str, body: str, priority: Priority) -> Delivery:
        self.calls.append((recipient, subject, body, priority))
        return Delivery(self.name, recipient, priority, body)


@pytest.mark.parametrize("channel", CHANNELS, ids=[c.name for c in CHANNELS])
@pytest.mark.parametrize(
    ("kind", "priority"),
    [(NormalNotification, Priority.NORMAL), (UrgentNotification, Priority.HIGH)],
)
def test_every_abstraction_works_with_every_implementor(
    kind: type[Notification], priority: Priority, channel: Channel
) -> None:
    delivery = kind(channel).send(RECIPIENT, SUBJECT, BODY)
    assert delivery.channel == channel.name
    assert delivery.recipient == RECIPIENT
    assert delivery.priority is priority
    assert SUBJECT in delivery.payload and BODY in delivery.payload


def test_urgency_crosses_the_bridge_in_each_implementors_own_vocabulary() -> None:
    email = UrgentNotification(EmailChannel()).send(RECIPIENT, SUBJECT, BODY)
    assert email.payload == f"Subject: {URGENT_MARKER}{SUBJECT}\nX-Priority: 1\n\n{BODY}"
    sms = UrgentNotification(SmsChannel()).send(RECIPIENT, SUBJECT, BODY)
    assert sms.payload == f"{URGENT_MARKER}{SUBJECT}: {BODY}"  # no header to set, the marker carries it
    push = UrgentNotification(PushChannel()).send(RECIPIENT, SUBJECT, BODY)
    assert json.loads(push.payload) == {
        "title": f"{URGENT_MARKER}{SUBJECT}",
        "body": BODY,
        "time_sensitive": True,
    }


def test_implementor_is_swapped_while_the_abstraction_lives() -> None:
    urgent = UrgentNotification(EmailChannel())
    assert urgent.send(RECIPIENT, SUBJECT, BODY).channel == "email"
    after_hours = SmsChannel()
    urgent.channel = after_hours
    assert urgent.channel is after_hours
    assert urgent.send(RECIPIENT, SUBJECT, BODY).channel == "sms"


def test_new_implementor_needs_no_base_class_and_new_abstraction_needs_no_channel_change() -> None:
    recording = RecordingChannel()
    assert isinstance(recording, Channel)  # structural: the shape is the contract
    digest = DigestNotification(recording)
    digest.send_digest(RECIPIENT, [("a", "1"), ("b", "2")])
    assert recording.calls == [(RECIPIENT, "Digest: 2 alerts", "- a: 1\n- b: 2", Priority.NORMAL)]
    UrgentNotification(recording).send(RECIPIENT, SUBJECT, BODY)
    assert recording.calls[-1] == (RECIPIENT, f"{URGENT_MARKER}{SUBJECT}", BODY, Priority.HIGH)


def test_each_side_keeps_its_own_rules() -> None:
    with pytest.raises(ValidationError):
        DigestNotification(EmailChannel()).send_digest(RECIPIENT, [])
    truncated = UrgentNotification(SmsChannel()).send(RECIPIENT, SUBJECT, "x" * 200)
    assert len(truncated.payload) == SMS_MAX_CHARS
    assert truncated.payload.startswith(URGENT_MARKER)  # the abstraction's marker survives the cut


@pytest.mark.parametrize("channel", CHANNELS, ids=[c.name for c in CHANNELS])
def test_value_object_with_a_callable_implementor_matches_the_class_form(channel: Channel) -> None:
    urgent = Alert(channel.deliver, Priority.HIGH, URGENT_MARKER)
    assert urgent.send(RECIPIENT, SUBJECT, BODY) == UrgentNotification(channel).send(
        RECIPIENT, SUBJECT, BODY
    )
    normal = Alert(channel.deliver)
    assert normal.send(RECIPIENT, SUBJECT, BODY) == NormalNotification(channel).send(
        RECIPIENT, SUBJECT, BODY
    )
