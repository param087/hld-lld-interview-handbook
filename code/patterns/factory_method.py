"""Factory Method: let a creator decide which concrete class to build.

The running example is a notification service that sends through email, SMS or
push. ``NotificationSenderFactory`` (the Creator) maps a ``Channel`` to the
callable that builds the matching ``Sender`` (the Product), so the code that
sends never names ``EmailSender`` or ``SmsSender``. The second section shows the
Gang of Four shape, where the factory method is a hook on an abstract creator,
and the third the Pythonic forms: a ``classmethod`` constructor and
``__init_subclass__`` auto-registration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any, ClassVar, Protocol, runtime_checkable

from common import ConflictError, NotFoundError, ValidationError

SMS_MAX_CHARS = 160


# --8<-- [start:products]
class Channel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    IN_APP = "in_app"
    WEBHOOK = "webhook"


@dataclass(frozen=True, slots=True)
class Notification:
    """What the caller wants delivered. It knows nothing about channels."""

    recipient: str
    subject: str
    body: str

    @classmethod
    def from_template(cls, recipient: str, subject: str, body: str, **fields: str) -> Notification:
        """A classmethod constructor: the same class, a different way in (``datetime.fromtimestamp``)."""
        return cls(recipient, subject.format(**fields), body.format(**fields))


@dataclass(frozen=True, slots=True)
class Receipt:
    """Proof of a send. Every sender returns one, so callers stay channel-agnostic."""

    channel: Channel
    recipient: str
    rendered: str


@runtime_checkable
class Sender(Protocol):
    """The Product interface. Concrete senders qualify by shape, not by inheritance."""

    def send(self, notification: Notification) -> Receipt: ...


@dataclass(frozen=True, slots=True)
class EmailSender:
    sender_address: str = "noreply@example.com"

    def send(self, notification: Notification) -> Receipt:
        rendered = (
            f"email from {self.sender_address} to {notification.recipient}: "
            f"[{notification.subject}] {notification.body}"
        )
        return Receipt(Channel.EMAIL, notification.recipient, rendered)


@dataclass(frozen=True, slots=True)
class SmsSender:
    sender_number: str = "+10000000000"
    max_chars: int = SMS_MAX_CHARS

    def send(self, notification: Notification) -> Receipt:
        text = f"{notification.subject}: {notification.body}"
        if len(text) > self.max_chars:
            text = text[: self.max_chars - 3] + "..."
        rendered = f"sms from {self.sender_number} to {notification.recipient}: {text}"
        return Receipt(Channel.SMS, notification.recipient, rendered)


@dataclass(frozen=True, slots=True)
class PushSender:
    app_id: str = "handbook"

    def send(self, notification: Notification) -> Receipt:
        rendered = f"push via {self.app_id} to {notification.recipient}: {notification.subject}"
        return Receipt(Channel.PUSH, notification.recipient, rendered)


# --8<-- [end:products]


# --8<-- [start:factory]
type SenderBuilder = Callable[[], Sender]


class NotificationSenderFactory:
    """The Creator: turns a channel into a sender without the caller naming a class.

    The registry maps each ``Channel`` to a zero-argument callable. A class is such
    a callable, and so is ``partial(EmailSender, sender_address=...)``, which is
    how configuration reaches a product without the factory knowing about it.
    The factory is an instance rather than a module global: ``main`` builds one and
    a test builds its own, with stubs. Registration happens at composition time,
    before any thread calls ``create``, so no lock is involved.
    """

    def __init__(self, builders: Mapping[Channel, SenderBuilder] | None = None) -> None:
        self._builders: dict[Channel, SenderBuilder] = dict(builders or {})

    @classmethod
    def with_defaults(cls) -> NotificationSenderFactory:
        return cls({Channel.EMAIL: EmailSender, Channel.SMS: SmsSender, Channel.PUSH: PushSender})

    def register(self, channel: Channel, builder: SenderBuilder) -> None:
        if channel in self._builders:
            raise ConflictError(f"a sender for {channel.value!r} is already registered")
        self._builders[channel] = builder

    def create(self, channel: Channel | str) -> Sender:
        try:
            key = Channel(channel)
        except ValueError:
            raise ValidationError(f"unknown channel {channel!r}") from None
        try:
            builder = self._builders[key]
        except KeyError:
            raise NotFoundError(f"no sender registered for {key.value!r}") from None
        return builder()

    def channels(self) -> tuple[Channel, ...]:
        return tuple(self._builders)


class NotificationService:
    """The client. It depends on the factory and on ``Sender``; it never imports a sender class."""

    def __init__(self, factory: NotificationSenderFactory) -> None:
        self._factory = factory

    def notify(self, channel: Channel | str, notification: Notification) -> Receipt:
        return self._factory.create(channel).send(notification)


# --8<-- [end:factory]


# --8<-- [start:creator]
class Notifier(ABC):
    """The Gang of Four shape: the factory method is a hook on an abstract creator.

    ``notify`` is written once against ``Sender``; each subclass decides which
    product ``create_sender`` returns. The choice is welded to a class hierarchy,
    which is why the registry form is the one to draw first.
    """

    def notify(self, recipient: str, subject: str, body: str) -> Receipt:
        return self.create_sender().send(Notification(recipient, subject, body))

    @abstractmethod
    def create_sender(self) -> Sender: ...


class EmailNotifier(Notifier):
    def __init__(self, sender_address: str) -> None:
        self._sender_address = sender_address

    def create_sender(self) -> Sender:
        return EmailSender(self._sender_address)


class SmsNotifier(Notifier):
    def create_sender(self) -> Sender:
        return SmsSender()


# --8<-- [end:creator]


# --8<-- [start:pythonic]
class RegisteredSender(ABC):
    """``__init_subclass__`` auto-registration: the class statement is the registration.

    ``class InAppSender(RegisteredSender, channel=Channel.IN_APP)`` adds the class to
    the registry; there is no list to forget to update. The costs: the registry is
    process-wide, so a subclass defined in a test stays registered until removed,
    and import order decides what exists, so a subclass in a module nobody imports
    is never registered.
    """

    _registry: ClassVar[dict[Channel, type[RegisteredSender]]] = {}
    channel: ClassVar[Channel]

    def __init_subclass__(cls, *, channel: Channel, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.channel = channel
        RegisteredSender._registry[channel] = cls

    @classmethod
    def for_channel(cls, channel: Channel | str) -> RegisteredSender:
        try:
            return cls._registry[Channel(channel)]()
        except (ValueError, KeyError):
            raise NotFoundError(f"no registered sender for {channel!r}") from None

    @classmethod
    def registered(cls) -> tuple[Channel, ...]:
        return tuple(cls._registry)

    @abstractmethod
    def send(self, notification: Notification) -> Receipt: ...


class InAppSender(RegisteredSender, channel=Channel.IN_APP):
    def send(self, notification: Notification) -> Receipt:
        rendered = f"inbox of {notification.recipient}: {notification.subject}"
        return Receipt(Channel.IN_APP, notification.recipient, rendered)


class WebhookSender(RegisteredSender, channel=Channel.WEBHOOK):
    def send(self, notification: Notification) -> Receipt:
        rendered = f"POST {notification.recipient} {{subject: {notification.subject}}}"
        return Receipt(Channel.WEBHOOK, notification.recipient, rendered)


# --8<-- [end:pythonic]


def main() -> None:
    factory = NotificationSenderFactory.with_defaults()
    service = NotificationService(factory)
    note = Notification.from_template(
        "ana@example.com", "Order {order} shipped", "It arrives on {day}.", order="A-42", day="Tuesday"
    )
    print("--- one call site, three products, chosen by a key ---")
    for channel in factory.channels():
        print(f"{channel.value:>5}: {service.notify(channel, note).rendered}")

    print("--- configuration travels in the builder; the factory never sees it ---")
    alerts = NotificationSenderFactory({Channel.EMAIL: partial(EmailSender, "alerts@example.com")})
    print(alerts.create("email").send(note).rendered)

    print("--- a misspelled channel and an unregistered one fail differently ---")
    for channel in ("fax", "in_app"):
        try:
            factory.create(channel)
        except (ValidationError, NotFoundError) as exc:
            print(f"{type(exc).__name__}: {exc}")

    print("--- GoF shape: the factory method is a hook on the creator ---")
    notifier: Notifier = EmailNotifier("billing@example.com")
    print(notifier.notify("ana@example.com", "Invoice ready", "Total 12.50 USD.").rendered)

    print("--- __init_subclass__: the class statement is the registration ---")
    print(f"registered: {[channel.value for channel in RegisteredSender.registered()]}")
    print(RegisteredSender.for_channel("in_app").send(note).rendered)


if __name__ == "__main__":
    main()
