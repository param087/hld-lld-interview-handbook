"""Bridge: keep two hierarchies apart so each can grow without the other.

The running example is sending alerts. ``Notification`` (the Abstraction) decides
*what* is said: a normal alert, an urgent one with a marker and a high priority,
or a digest that folds several alerts into one message. ``Channel`` (the
Implementor) decides *how* it travels: e-mail, SMS or push, each with its own
payload format and limits. Every notification holds *a* channel and delegates
delivery to it, so a new channel costs one class and a new kind of notification
costs one class. Subclassing one hierarchy from the other would cost one class
per combination.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from common import ValidationError

SMS_MAX_CHARS = 160
URGENT_MARKER = "[URGENT] "


# --8<-- [start:implementor]
class Priority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class Delivery:
    """The receipt a channel returns: what went over the wire, and where."""

    channel: str
    recipient: str
    priority: Priority
    payload: str


@runtime_checkable
class Channel(Protocol):
    """The Implementor: how a message reaches a device. It knows nothing about what is said."""

    name: str

    def deliver(self, recipient: str, subject: str, body: str, priority: Priority) -> Delivery: ...


class EmailChannel:
    """Has a subject line and a priority header; no length limit."""

    name = "email"

    def deliver(self, recipient: str, subject: str, body: str, priority: Priority) -> Delivery:
        x_priority = 1 if priority is Priority.HIGH else 3
        payload = f"Subject: {subject}\nX-Priority: {x_priority}\n\n{body}"
        return Delivery(self.name, recipient, priority, payload)


class SmsChannel:
    """No subject line, no priority flag, a hard length limit: the subject is folded into the text."""

    name = "sms"

    def deliver(self, recipient: str, subject: str, body: str, priority: Priority) -> Delivery:
        payload = f"{subject}: {body}"[:SMS_MAX_CHARS]
        return Delivery(self.name, recipient, priority, payload)


class PushChannel:
    """A JSON document with a title, a body and a time-sensitive flag."""

    name = "push"

    def deliver(self, recipient: str, subject: str, body: str, priority: Priority) -> Delivery:
        document = {"title": subject, "body": body, "time_sensitive": priority is Priority.HIGH}
        return Delivery(self.name, recipient, priority, json.dumps(document))


# --8<-- [end:implementor]


# --8<-- [start:abstraction]
class Notification(ABC):
    """The Abstraction: what is being said. Holds *a* Channel and delegates the how.

    Its interface is higher-level than the Implementor's: callers pass a subject
    and a body; the notification adds the priority and any decoration before the
    call crosses the bridge. The channel can be swapped while the object lives.
    """

    def __init__(self, channel: Channel) -> None:
        self._channel = channel

    @property
    def channel(self) -> Channel:
        return self._channel

    @channel.setter
    def channel(self, channel: Channel) -> None:
        self._channel = channel

    @property
    @abstractmethod
    def priority(self) -> Priority: ...

    def subject_for(self, subject: str) -> str:
        """Hook for refined abstractions; the default leaves the subject alone."""
        return subject

    def send(self, recipient: str, subject: str, body: str) -> Delivery:
        return self._channel.deliver(recipient, self.subject_for(subject), body, self.priority)


class NormalNotification(Notification):
    @property
    def priority(self) -> Priority:
        return Priority.NORMAL


class UrgentNotification(Notification):
    """Marks the subject and asks for high priority; how that shows is the channel's business."""

    @property
    def priority(self) -> Priority:
        return Priority.HIGH

    def subject_for(self, subject: str) -> str:
        return f"{URGENT_MARKER}{subject}"


class DigestNotification(Notification):
    """A refined abstraction with a wider interface: many alerts become one message."""

    @property
    def priority(self) -> Priority:
        return Priority.NORMAL

    def send_digest(self, recipient: str, alerts: Sequence[tuple[str, str]]) -> Delivery:
        if not alerts:
            raise ValidationError("a digest needs at least one alert")
        subject = f"Digest: {len(alerts)} alerts"
        body = "\n".join(f"- {title}: {detail}" for title, detail in alerts)
        return self.send(recipient, subject, body)


# --8<-- [end:abstraction]


# --8<-- [start:pythonic]
# When the refined abstractions differ only by data they collapse into one value object,
# and a one-method implementor is a callable: a bound method such as ``SmsChannel().deliver``.
type Deliver = Callable[[str, str, str, Priority], Delivery]


@dataclass(frozen=True, slots=True)
class Alert:
    deliver: Deliver
    priority: Priority = Priority.NORMAL
    marker: str = ""

    def send(self, recipient: str, subject: str, body: str) -> Delivery:
        return self.deliver(recipient, f"{self.marker}{subject}", body, self.priority)


# --8<-- [end:pythonic]


def main() -> None:
    recipient = "user-42"
    subject, body = "Disk almost full", "db-3 at 95%"
    channels: list[Channel] = [EmailChannel(), SmsChannel()]
    kinds: list[type[Notification]] = [NormalNotification, UrgentNotification]

    print("--- 2 abstractions x 2 implementors: 4 behaviours from 4 classes ---")
    for kind in kinds:
        for channel in channels:
            delivery = kind(channel).send(recipient, subject, body)
            print(f"{kind.__name__} via {delivery.channel}: {delivery.payload!r}")

    print("--- a third implementor: PushChannel, no change on the abstraction side ---")
    delivery = UrgentNotification(PushChannel()).send(recipient, subject, body)
    print(f"UrgentNotification via push: {delivery.payload}")

    print("--- a third abstraction: DigestNotification, no change on the implementor side ---")
    alerts = [(subject, body), ("Cert expiring", "api.example.com in 3 days")]
    delivery = DigestNotification(EmailChannel()).send_digest(recipient, alerts)
    print(f"DigestNotification via email: {delivery.payload!r}")

    print("--- swap the implementor under a live abstraction ---")
    urgent = UrgentNotification(EmailChannel())
    print(f"office hours: {urgent.send(recipient, subject, body).channel}")
    urgent.channel = SmsChannel()
    print(f"after hours:  {urgent.send(recipient, subject, body).channel}")

    print("--- each implementor keeps its own contract: SMS truncates at 160 characters ---")
    delivery = urgent.send(recipient, subject, "x" * 200)
    print(f"payload length {len(delivery.payload)}, starts with {delivery.payload[:25]!r}")

    print("--- Pythonic variant: a value object holding a callable implementor ---")
    alert = Alert(SmsChannel().deliver, Priority.HIGH, URGENT_MARKER)
    print(f"Alert via sms: {alert.send(recipient, subject, body).payload!r}")

    try:
        DigestNotification(EmailChannel()).send_digest(recipient, [])
    except ValidationError as exc:
        print(f"rejected: {exc}")


if __name__ == "__main__":
    main()
