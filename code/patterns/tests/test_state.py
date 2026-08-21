"""State: the current state decides which events are legal, and moves the machine itself."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError
from patterns.state import (
    TRANSITIONS,
    Event,
    Slot,
    Status,
    VendingMachine,
    next_status,
    next_status_guarded,
)

Prepare = Callable[[VendingMachine], object]


def machine(cola: int = 2, chips: int = 0) -> VendingMachine:
    return VendingMachine(
        {"A1": Slot("cola", Money.of("1.50"), cola), "B2": Slot("chips", Money.of("1.00"), chips)}
    )


def with_credit(m: VendingMachine) -> None:
    m.insert_money(Money.of("2.00"))


def mid_dispense(m: VendingMachine) -> None:
    m.insert_money(Money.of("2.00"))
    m.select("A1")


def offline(m: VendingMachine) -> None:
    m.disable()


def test_happy_path_walks_idle_has_money_dispensing_idle_and_returns_change() -> None:
    m = machine()
    m.insert_money(Money.of("1.00"))
    m.insert_money(Money.of("1.00"))
    assert (m.state_name, m.balance) == ("HasMoney", Money.of("2.00"))
    m.select("A1")
    assert m.state_name == "Dispensing"
    assert m.dispense() == ("cola", Money.of("0.50"))
    assert (m.state_name, m.balance, m.quantity("A1")) == ("Idle", Money(0), 1)
    assert m.transitions == [("Idle", "HasMoney"), ("HasMoney", "Dispensing"), ("Dispensing", "Idle")]


@pytest.mark.parametrize(
    ("prepare", "event", "message"),
    [
        (lambda m: None, "select", "cannot select while Idle"),
        (lambda m: None, "dispense", "cannot dispense while Idle"),
        (lambda m: None, "cancel", "cannot cancel while Idle"),
        (lambda m: None, "enable", "cannot enable while Idle"),
        (with_credit, "dispense", "cannot dispense while HasMoney"),
        (with_credit, "disable", "cannot disable while HasMoney"),
        (mid_dispense, "insert_money", "cannot insert money while Dispensing"),
        (mid_dispense, "cancel", "cannot cancel while Dispensing"),
        (offline, "insert_money", "cannot insert money while OutOfService"),
        (offline, "select", "cannot select while OutOfService"),
    ],
)
def test_events_that_make_no_sense_in_the_current_state_are_rejected(
    prepare: Prepare, event: str, message: str
) -> None:
    m = machine()
    prepare(m)
    before = m.state_name
    args = {"insert_money": (Money(100),), "select": ("A1",)}.get(event, ())
    with pytest.raises(InvalidStateError, match=message):
        getattr(m, event)(*args)
    assert m.state_name == before  # a rejected event never moves the machine


def test_a_failed_selection_keeps_the_state_and_the_balance() -> None:
    m = machine()
    m.insert_money(Money.of("0.50"))
    with pytest.raises(ValidationError, match="insert 1.00 USD more"):
        m.select("A1")
    with pytest.raises(ConflictError, match="sold out"):
        m.select("B2")
    with pytest.raises(NotFoundError):
        m.select("Z9")
    assert (m.state_name, m.balance) == ("HasMoney", Money.of("0.50"))
    assert m.transitions == [("Idle", "HasMoney")]


def test_cancel_refunds_and_a_fault_while_dispensing_refunds_too() -> None:
    m = machine()
    m.insert_money(Money.of("0.75"))
    assert m.cancel() == Money.of("0.75")
    assert (m.state_name, m.balance) == ("Idle", Money(0))

    m.insert_money(Money.of("2.00"))
    m.select("A1")
    assert m.disable() == Money.of("2.00")  # a jam: the money comes back, the product stays
    assert (m.state_name, m.quantity("A1")) == ("OutOfService", 2)
    m.enable()
    assert m.state_name == "Idle"


@pytest.mark.parametrize("cents", [0, -50])
def test_non_positive_coins_are_validated_before_any_state_sees_them(cents: int) -> None:
    m = machine()
    with pytest.raises(ValidationError):
        m.insert_money(Money(cents))
    assert m.transitions == []


def test_racing_selections_let_exactly_one_thread_into_dispensing() -> None:
    m = machine(cola=5)
    m.insert_money(Money.of("2.00"))

    def try_select(_: int) -> bool:
        try:
            m.select("A1")
        except InvalidStateError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(try_select, range(64)))
    assert outcomes.count(True) == 1
    assert m.state_name == "Dispensing"
    assert m.dispense() == ("cola", Money.of("0.50"))
    assert m.quantity("A1") == 4


def test_the_table_and_the_classes_describe_the_same_lifecycle() -> None:
    class_name = {
        Status.IDLE: "Idle",
        Status.HAS_MONEY: "HasMoney",
        Status.DISPENSING: "Dispensing",
        Status.OUT_OF_SERVICE: "OutOfService",
    }
    script: list[tuple[Event, Prepare]] = [
        (Event.INSERT, lambda m: m.insert_money(Money(200))),
        (Event.INSERT, lambda m: m.insert_money(Money(100))),
        (Event.SELECT, lambda m: m.select("A1")),
        (Event.DISPENSE, lambda m: m.dispense()),
        (Event.INSERT, lambda m: m.insert_money(Money(50))),
        (Event.CANCEL, lambda m: m.cancel()),
        (Event.DISABLE, lambda m: m.disable()),
        (Event.ENABLE, lambda m: m.enable()),
    ]
    m = machine()
    status = Status.IDLE
    for event, action in script:
        action(m)
        status = next_status(status, event)
        assert class_name[status] == m.state_name


@pytest.mark.parametrize("status", list(Status))
def test_the_table_rejects_every_pair_it_does_not_list(status: Status) -> None:
    for event in Event:
        if (status, event) in TRANSITIONS:
            assert next_status(status, event) is TRANSITIONS[(status, event)]
        else:
            with pytest.raises(InvalidStateError, match=f"cannot {event} while {status}"):
                next_status(status, event)


def test_guarded_transition_checks_the_balance_before_consulting_the_table() -> None:
    price = Money.of("1.50")
    with pytest.raises(ValidationError, match="insert 1.00 USD more"):
        next_status_guarded(Status.HAS_MONEY, Event.SELECT, balance=Money.of("0.50"), price=price)
    assert next_status_guarded(Status.HAS_MONEY, Event.SELECT, balance=price, price=price) is Status.DISPENSING
    with pytest.raises(InvalidStateError):
        next_status_guarded(Status.IDLE, Event.SELECT, balance=price, price=price)
