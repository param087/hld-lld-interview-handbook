"""Factory Method: a key picks the product, the client never names a class, and the Pythonic forms."""

from functools import partial

import pytest

from common import ConflictError, NotFoundError, ValidationError
from patterns.factory_method import (
    Channel,
    EmailNotifier,
    EmailSender,
    InAppSender,
    Notification,
    NotificationSenderFactory,
    NotificationService,
    Notifier,
    PushSender,
    Receipt,
    RegisteredSender,
    Sender,
    SmsNotifier,
    SmsSender,
    WebhookSender,
)

NOTE = Notification("ana@example.com", "Order shipped", "It arrives on Tuesday.")


@pytest.mark.parametrize(
    ("channel", "expected_type"),
    [
        (Channel.EMAIL, EmailSender),
        ("sms", SmsSender),  # a raw string from a request is normalised once, inside create
        (Channel.PUSH, PushSender),
    ],
)
def test_factory_builds_the_product_for_the_key(channel: Channel | str, expected_type: type) -> None:
    sender = NotificationSenderFactory.with_defaults().create(channel)
    assert isinstance(sender, expected_type)
    assert isinstance(sender, Sender)  # by shape: no sender inherits from the Protocol
    assert sender.send(NOTE).channel is Channel(channel)


def test_unknown_and_unregistered_channels_fail_differently() -> None:
    factory = NotificationSenderFactory.with_defaults()
    with pytest.raises(ValidationError):
        factory.create("fax")  # not a Channel at all: bad input
    with pytest.raises(NotFoundError):
        factory.create(Channel.IN_APP)  # a real channel nobody registered: bad configuration


def test_configuration_lives_in_the_builder_and_a_channel_registers_once() -> None:
    factory = NotificationSenderFactory()
    factory.register(Channel.EMAIL, partial(EmailSender, "alerts@example.com"))
    assert factory.create("email").send(NOTE).rendered.startswith("email from alerts@example.com")
    with pytest.raises(ConflictError):
        factory.register(Channel.EMAIL, EmailSender)
    assert factory.channels() == (Channel.EMAIL,)


def test_client_depends_on_the_factory_not_on_sender_classes() -> None:
    class RecordingSender:
        def __init__(self) -> None:
            self.seen: list[Notification] = []

        def send(self, notification: Notification) -> Receipt:
            self.seen.append(notification)
            return Receipt(Channel.PUSH, notification.recipient, "recorded")

    spy = RecordingSender()
    service = NotificationService(NotificationSenderFactory({Channel.PUSH: lambda: spy}))
    assert service.notify("push", NOTE).rendered == "recorded"
    assert spy.seen == [NOTE]


def test_gof_creator_lets_each_subclass_choose_the_product() -> None:
    with pytest.raises(TypeError):
        Notifier()  # type: ignore[abstract]
    email = EmailNotifier("billing@example.com").notify("ana@example.com", "Invoice", "12.50 USD")
    sms = SmsNotifier().notify("+15550100", "Invoice", "12.50 USD")
    assert (email.channel, sms.channel) == (Channel.EMAIL, Channel.SMS)
    assert "billing@example.com" in email.rendered


def test_init_subclass_registers_at_class_creation_and_the_registry_is_global() -> None:
    assert set(RegisteredSender.registered()) >= {Channel.IN_APP, Channel.WEBHOOK}
    assert isinstance(RegisteredSender.for_channel("in_app"), InAppSender)
    assert isinstance(RegisteredSender.for_channel(Channel.WEBHOOK), WebhookSender)
    with pytest.raises(NotFoundError):
        RegisteredSender.for_channel("sms")

    class TestOnlySender(RegisteredSender, channel=Channel.SMS):
        def send(self, notification: Notification) -> Receipt:
            return Receipt(Channel.SMS, notification.recipient, "test double")

    try:
        assert isinstance(RegisteredSender.for_channel("sms"), TestOnlySender)
    finally:
        RegisteredSender._registry.pop(Channel.SMS)  # the cost of a global registry: tests must clean up
    with pytest.raises(NotFoundError):
        RegisteredSender.for_channel("sms")


def test_sms_sender_truncates_to_its_limit() -> None:
    long_note = Notification("+15550100", "Subject", "x" * 500)
    receipt = SmsSender(max_chars=40).send(long_note)
    text = receipt.rendered.split(": ", 1)[1]
    assert len(text) == 40 and text.endswith("...")


def test_classmethod_constructor_fills_the_template() -> None:
    note = Notification.from_template("ana@example.com", "Order {order}", "Arrives {day}.", order="A-42", day="Tuesday")
    assert note == Notification("ana@example.com", "Order A-42", "Arrives Tuesday.")
