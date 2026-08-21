"""Decorator: same interface in and out, behaviour stacks, order is semantics, wraps keeps identity."""

import pytest

from common import Money, ValidationError
from patterns.decorator import (
    AddOn,
    AuditingSender,
    Beverage,
    Espresso,
    ExtraShot,
    Milk,
    RetryingSender,
    Sender,
    SendError,
    SmtpSender,
    Syrup,
    audited,
    retry,
)


class RecordingSender:
    """A Component written in a test: no base class, and it sees exactly what the decorators pass on."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send(self, recipient: str, message: str) -> str:
        self.calls.append((recipient, message))
        return f"rec-{len(self.calls)}"


def no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.parametrize(
    ("drink", "description", "cost"),
    [
        (Espresso(), "espresso", "2.00"),
        (Milk(Espresso()), "espresso, milk", "2.50"),
        (ExtraShot(ExtraShot(Espresso())), "espresso, extra shot, extra shot", "3.60"),
        (Syrup(Milk(Espresso()), flavour="hazelnut"), "espresso, milk, hazelnut syrup", "2.90"),
        (Milk(Syrup(ExtraShot(Espresso()))), "espresso, extra shot, vanilla syrup, milk", "3.70"),
    ],
)
def test_add_ons_stack_in_any_order_and_any_number(drink: Beverage, description: str, cost: str) -> None:
    assert drink.description() == description
    assert drink.cost() == Money.of(cost)


def test_a_decorated_drink_is_still_a_beverage_and_the_base_add_on_changes_nothing() -> None:
    drink = Milk(Espresso())
    assert isinstance(drink, Beverage) and isinstance(drink, AddOn)
    assert AddOn(Espresso()).description() == Espresso().description()
    assert AddOn(Espresso()).cost() == Espresso().cost()
    assert Milk(ExtraShot(Espresso())).cost() == ExtraShot(Milk(Espresso())).cost()  # cost commutes
    assert Milk(ExtraShot(Espresso())).description() != ExtraShot(Milk(Espresso())).description()


def test_retrying_sender_absorbs_transient_failures_with_a_backoff_schedule() -> None:
    delays: list[float] = []
    sender = RetryingSender(SmtpSender(fail_first=2), attempts=3, base_delay=0.1, sleep=delays.append)
    assert sender.send("user-42", "hi") == "smtp-1"
    assert delays == [0.1, 0.2]  # one sleep per failure, doubling; none after the success


def test_retrying_sender_gives_up_with_the_original_error_and_validates_its_budget() -> None:
    delays: list[float] = []
    sender = RetryingSender(SmtpSender(fail_first=5), attempts=3, sleep=delays.append)
    with pytest.raises(SendError, match="connection reset"):
        sender.send("user-42", "hi")
    assert delays == [0.1, 0.2]  # three attempts, two waits
    with pytest.raises(ValidationError):
        RetryingSender(SmtpSender(), attempts=0)


def test_decorators_are_transparent_to_the_component_and_to_the_client() -> None:
    recording = RecordingSender()
    sender: Sender = AuditingSender(RetryingSender(recording, sleep=no_sleep), sink=lambda _: None)
    assert isinstance(sender, Sender)
    assert sender.send("user-42", "hello") == "rec-1"
    assert recording.calls == [("user-42", "hello")]  # arguments and receipt pass through untouched


def test_stacking_order_decides_what_the_audit_trail_records() -> None:
    outside: list[str] = []
    flaky: Sender = RetryingSender(SmtpSender(fail_first=2), sleep=no_sleep)
    AuditingSender(flaky, outside.append).send("user-42", "hello")
    assert outside == ["user-42: ok (smtp-1)"]

    inside: list[str] = []
    audited_transport: Sender = AuditingSender(SmtpSender(fail_first=2), inside.append)
    RetryingSender(audited_transport, sleep=no_sleep).send("user-42", "hello")
    assert inside == [
        "user-42: failed (connection reset)",
        "user-42: failed (connection reset)",
        "user-42: ok (smtp-1)",
    ]


def test_function_decorators_behave_like_the_classes_and_wraps_keeps_the_identity() -> None:
    transport = SmtpSender(fail_first=2)
    delays: list[float] = []

    @retry(attempts=3, sleep=delays.append)
    def send_email(recipient: str, message: str) -> str:
        """Send through the transport."""
        return transport.send(recipient, message)

    assert send_email("user-42", "hi") == "smtp-1"
    assert delays == [0.1, 0.2]
    assert send_email.__name__ == "send_email"
    assert send_email.__doc__ == "Send through the transport."
    assert send_email.__wrapped__("user-42", "hi") == "smtp-2"  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        retry(attempts=0)


def test_function_decorators_stack_with_the_same_order_semantics() -> None:
    outside: list[str] = []

    @audited(outside.append)
    @retry(attempts=3, sleep=no_sleep)
    def send_outside(recipient: str, transport: SmtpSender) -> str:
        return transport.send(recipient, "x")

    assert send_outside("user-42", SmtpSender(fail_first=2)) == "smtp-1"
    assert outside == ["send_outside: ok (smtp-1)"]

    inside: list[str] = []

    @retry(attempts=3, sleep=no_sleep)
    @audited(inside.append)
    def send_inside(recipient: str, transport: SmtpSender) -> str:
        return transport.send(recipient, "x")

    assert send_inside("user-42", SmtpSender(fail_first=2)) == "smtp-1"
    assert inside == [
        "send_inside: failed (connection reset)",
        "send_inside: failed (connection reset)",
        "send_inside: ok (smtp-1)",
    ]
