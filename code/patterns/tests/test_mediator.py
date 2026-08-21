"""Mediator: panels and cars never reference each other; the controller owns dispatch and the lamp rule."""

from collections import defaultdict

import pytest

from common import ConflictError, NotFoundError, ValidationError
from patterns.mediator import ChatRoom, Direction, Elevator, ElevatorController, HallPanel


def make_bank() -> ElevatorController:
    return ElevatorController([Elevator("A", floor=0), Elevator("B", floor=8)], floors=10)


def run(controller: ElevatorController, ticks: int) -> None:
    for _ in range(ticks):
        controller.tick()


def test_the_nearest_idle_car_takes_the_call_and_its_arrival_clears_the_lamp() -> None:
    bank = make_bank()
    a, b = bank.cars
    bank.panel(3).press(Direction.UP)
    assert a.stops == {3} and b.idle
    assert bank.panel(3).lit == {Direction.UP}
    run(bank, 2)
    assert (a.floor, a.direction) == (2, Direction.UP)
    assert bank.panel(3).lit == {Direction.UP}  # not there yet
    run(bank, 1)
    assert a.floor == 3 and a.idle
    assert bank.panel(3).lit == set()
    assert bank.log == ["call 3 up: assigned to A", "A arrived at 3: cleared up"]


def test_a_car_already_heading_that_way_beats_a_farther_idle_car() -> None:
    bank = ElevatorController([Elevator("A", floor=0), Elevator("B", floor=9)], floors=10)
    a, b = bank.cars
    a.add_stop(8)  # a cabin request: the car's own business, no mediator involved
    run(bank, 2)
    assert (a.floor, a.direction) == (2, Direction.UP)
    bank.panel(5).press(Direction.UP)  # A is heading up, 3 away; B is idle, 4 away
    assert a.stops == {5, 8} and b.idle
    bank.panel(6).press(Direction.DOWN)  # A heads up, so it does not count as heading; B is idle
    assert b.stops == {6}


def test_a_second_press_is_ignored_and_another_car_does_not_clear_a_lamp_it_is_not_serving() -> None:
    bank = make_bank()
    a, b = bank.cars
    bank.panel(3).press(Direction.UP)
    bank.panel(3).press(Direction.UP)
    assert a.stops == {3}
    assert bank.log == ["call 3 up: assigned to A"]
    bank.car_arrived(b, 3)  # B stops at 3 for a cabin request of its own
    assert bank.panel(3).lit == {Direction.UP}
    bank.car_arrived(a, 3)
    assert bank.panel(3).lit == set()
    assert bank.log[-2:] == ["B arrived at 3: cleared nothing", "A arrived at 3: cleared up"]


def test_colleagues_depend_only_on_the_mediator_protocol() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def hall_call(self, floor: int, direction: Direction) -> None:
            self.calls.append(("call", floor, direction))

        def car_arrived(self, car: Elevator, floor: int) -> None:
            self.calls.append(("arrived", car.name, floor))

    recorder = Recorder()
    panel = HallPanel(4, recorder)
    panel.press(Direction.DOWN)
    car = Elevator("X", floor=4)
    car.attach(recorder)
    car.add_stop(4)
    car.step()
    assert recorder.calls == [("call", 4, Direction.DOWN), ("arrived", "X", 4)]
    panel.clear(Direction.DOWN)
    assert panel.lit == set()
    lone = Elevator("L", floor=0)  # a car with no mediator still moves; it has nobody to tell
    lone.add_stop(1)
    lone.step()
    assert (lone.floor, lone.idle) == (1, True)


def test_a_car_keeps_its_direction_while_stops_remain_ahead_then_turns_then_idles() -> None:
    car = Elevator("A", floor=5)
    car.add_stop(7)
    car.add_stop(2)
    trace: list[tuple[int, Direction | None]] = []
    for _ in range(8):
        car.step()
        trace.append((car.floor, car.direction))
    assert trace == [
        (6, Direction.UP),
        (7, Direction.UP),
        (6, Direction.DOWN),
        (5, Direction.DOWN),
        (4, Direction.DOWN),
        (3, Direction.DOWN),
        (2, Direction.DOWN),
        (2, None),
    ]


def test_the_controller_validates_its_inputs() -> None:
    with pytest.raises(ValidationError):
        ElevatorController([], floors=10)
    with pytest.raises(ValidationError):
        ElevatorController([Elevator("A")], floors=1)
    with pytest.raises(NotFoundError):
        make_bank().panel(42)


def test_the_chat_room_routes_broadcasts_direct_messages_and_blocks() -> None:
    room = ChatRoom()
    inbox: dict[str, list[str]] = defaultdict(list)
    for name in ("alice", "bob", "carol"):
        room.join(name, lambda sender, text, name=name: inbox[name].append(f"{sender}: {text}"))
    assert room.say("alice", "hi") == 2
    room.block("carol", "bob")
    assert room.say("bob", "yo") == 1
    assert inbox["carol"] == ["alice: hi"]
    assert room.say("bob", "psst", to="alice") == 1
    assert inbox["alice"] == ["bob: yo", "bob: psst"]
    room.leave("carol")
    assert room.say("alice", "bye") == 1
    assert inbox["bob"] == ["alice: hi", "alice: bye"]


def test_the_chat_room_rejects_strangers_and_duplicate_names() -> None:
    class Inbox:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def receive(self, sender: str, text: str) -> None:
            self.messages.append(f"{sender}: {text}")

    room = ChatRoom()
    inbox = Inbox()
    room.join("alice", inbox.receive)  # a bound method is a member too
    room.join("bob", lambda sender, text: None)
    with pytest.raises(ConflictError):
        room.join("alice", inbox.receive)
    with pytest.raises(NotFoundError):
        room.say("dave", "anyone?")
    with pytest.raises(NotFoundError):
        room.say("bob", "hello?", to="dave")
    assert room.say("bob", "hello") == 1
    assert inbox.messages == ["bob: hello"]
