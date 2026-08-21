"""Mediator: one object coordinates many colleagues so that none of them talk to each other.

The running example is an elevator bank. Hall panels and cars are the Colleagues:
a panel knows how to light a lamp, a car knows how to move and stop, and neither
knows the other exists. ``ElevatorController`` (the Mediator) receives hall calls,
chooses a car, and when a car reports an arrival it clears the lamps that call
served. Replace the controller and the dispatch policy changes; add a car and
nothing else changes. The second half restates the idea as a chat room over
plain callables, the lightweight form when the mediator routes more than it decides.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Protocol

from common import ConflictError, NotFoundError, ValidationError


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"


# --8<-- [start:colleagues]
class DispatchMediator(Protocol):
    """What a colleague may ask of the mediator. Colleagues never see each other, only this."""

    def hall_call(self, floor: int, direction: Direction) -> None: ...

    def car_arrived(self, car: Elevator, floor: int) -> None: ...


class HallPanel:
    """A colleague: the two buttons and lamps on one floor. It reports presses and lights lamps."""

    def __init__(self, floor: int, mediator: DispatchMediator) -> None:
        self.floor = floor
        self._mediator = mediator
        self.lit: set[Direction] = set()

    def press(self, direction: Direction) -> None:
        if direction in self.lit:
            return  # the call is already registered; pressing harder changes nothing
        self.lit.add(direction)
        self._mediator.hall_call(self.floor, direction)

    def clear(self, direction: Direction) -> None:
        self.lit.discard(direction)


class Elevator:
    """A colleague: moves one floor per tick towards its stops and reports every arrival.

    Cabin buttons call ``add_stop`` directly, because a car owns its own stops; hall
    calls reach it only through the mediator. The car keeps its direction while it
    has stops ahead (the LOOK rule) and otherwise turns around or goes idle.
    """

    def __init__(self, name: str, floor: int = 0) -> None:
        self.name = name
        self.floor = floor
        self.direction: Direction | None = None
        self._stops: set[int] = set()
        self._mediator: DispatchMediator | None = None

    def attach(self, mediator: DispatchMediator) -> None:
        self._mediator = mediator

    @property
    def stops(self) -> frozenset[int]:
        return frozenset(self._stops)

    @property
    def idle(self) -> bool:
        return not self._stops

    def add_stop(self, floor: int) -> None:
        self._stops.add(floor)

    def step(self) -> None:
        """One tick: open at the current floor if it is a stop, otherwise move one floor."""
        if not self._stops:
            self.direction = None
            return
        if self.floor in self._stops:
            self._arrive()
            return
        if self.direction is Direction.UP and not any(s > self.floor for s in self._stops):
            self.direction = Direction.DOWN
        elif self.direction is Direction.DOWN and not any(s < self.floor for s in self._stops):
            self.direction = Direction.UP
        elif self.direction is None:
            nearest = min(self._stops, key=lambda s: abs(s - self.floor))
            self.direction = Direction.UP if nearest > self.floor else Direction.DOWN
        self.floor += 1 if self.direction is Direction.UP else -1
        if self.floor in self._stops:
            self._arrive()

    def _arrive(self) -> None:
        self._stops.remove(self.floor)
        if self._mediator is not None:
            self._mediator.car_arrived(self, self.floor)


# --8<-- [end:colleagues]


# --8<-- [start:mediator]
class ElevatorController:
    """The Mediator: the only object that knows both the panels and the cars.

    It owns the two decisions that would otherwise be smeared across every
    colleague: which car answers a call, and which lamps an arrival clears.
    ``_pending`` remembers the assignment so an arrival of a *different* car at the
    same floor does not clear a lamp that car is not serving.
    """

    def __init__(self, cars: Iterable[Elevator], floors: int) -> None:
        self._cars = list(cars)
        if not self._cars or floors < 2:
            raise ValidationError("a controller needs at least one car and two floors")
        for car in self._cars:
            car.attach(self)
        self._panels = {floor: HallPanel(floor, self) for floor in range(floors)}
        self._pending: dict[tuple[int, Direction], Elevator] = {}
        self.log: list[str] = []

    def panel(self, floor: int) -> HallPanel:
        if floor not in self._panels:
            raise NotFoundError(f"there is no floor {floor}")
        return self._panels[floor]

    @property
    def cars(self) -> tuple[Elevator, ...]:
        return tuple(self._cars)

    def hall_call(self, floor: int, direction: Direction) -> None:
        car = min(self._cars, key=lambda candidate: self._cost(candidate, floor, direction))
        car.add_stop(floor)
        self._pending[(floor, direction)] = car
        self.log.append(f"call {floor} {direction}: assigned to {car.name}")

    def car_arrived(self, car: Elevator, floor: int) -> None:
        served = [key for key, assigned in self._pending.items() if assigned is car and key[0] == floor]
        for key in served:
            del self._pending[key]
            self._panels[floor].clear(key[1])
        cleared = ", ".join(f"{key[1]}" for key in served) or "nothing"
        self.log.append(f"{car.name} arrived at {floor}: cleared {cleared}")

    def tick(self) -> None:
        for car in self._cars:
            car.step()

    @staticmethod
    def _cost(car: Elevator, floor: int, direction: Direction) -> tuple[int, int, int]:
        """Nearest car that is idle or already heading that way wins; otherwise the least busy."""
        distance = abs(car.floor - floor)
        heading = car.direction is direction and (
            (direction is Direction.UP and car.floor <= floor)
            or (direction is Direction.DOWN and car.floor >= floor)
        )
        if car.idle or heading:
            return (0, distance, 0)
        return (1, len(car.stops), distance)


# --8<-- [end:mediator]


# --8<-- [start:hub]
# (sender, text) -> None: a member is nothing more than something that can receive.
type Receiver = Callable[[str, str], None]


class ChatRoom:
    """The lightweight mediator: members are callables, the room owns the routing policy.

    Members never hold references to each other; broadcast, direct messages and
    blocking are decided here, in one place. That policy is what tells a mediator
    apart from an event bus, which delivers by topic and decides nothing.
    """

    def __init__(self) -> None:
        self._members: dict[str, Receiver] = {}
        self._blocked: set[tuple[str, str]] = set()  # (who blocked, whom)

    def join(self, name: str, receive: Receiver) -> None:
        if name in self._members:
            raise ConflictError(f"{name!r} is already in the room")
        self._members[name] = receive

    def leave(self, name: str) -> None:
        self._members.pop(name, None)

    def block(self, blocker: str, blocked: str) -> None:
        self._blocked.add((blocker, blocked))

    def say(self, sender: str, text: str, to: str | None = None) -> int:
        """Broadcast, or a direct message when ``to`` is given. Returns how many members received it."""
        if sender not in self._members:
            raise NotFoundError(f"{sender!r} is not in the room")
        if to is not None and to not in self._members:
            raise NotFoundError(f"{to!r} is not in the room")
        recipients = [to] if to is not None else [name for name in self._members if name != sender]
        delivered = 0
        for name in recipients:
            if (name, sender) in self._blocked:
                continue
            self._members[name](sender, text)
            delivered += 1
        return delivered


# --8<-- [end:hub]


def _status(controller: ElevatorController) -> str:
    cars = "  ".join(
        f"{car.name}@{car.floor}{'' if car.direction is None else ' ' + car.direction}"
        f"{' ' + str(sorted(car.stops)) if car.stops else ''}"
        for car in controller.cars
    )
    lit = [f"{floor}{direction}" for floor in range(10) for direction in controller.panel(floor).lit]
    return f"{cars:<40} lamps: {', '.join(lit) or 'none'}"


def main() -> None:
    controller = ElevatorController([Elevator("A", floor=0), Elevator("B", floor=8)], floors=10)
    print("--- two cars, ten floors; panels and cars only ever talk to the controller ---")
    print(f"start:  {_status(controller)}")
    controller.panel(3).press(Direction.UP)
    controller.panel(7).press(Direction.DOWN)
    controller.panel(3).press(Direction.UP)  # a second press changes nothing
    print(f"calls:  {_status(controller)}")
    for tick in range(1, 5):
        controller.tick()
        if tick == 3:
            controller.cars[0].add_stop(6)  # a passenger boards A at 3 and presses 6: no mediator
        print(f"tick {tick}: {_status(controller)}")
    print("controller log:")
    for line in controller.log:
        print(f"  {line}")

    print("--- the chat room: the same shape with callables and a routing policy ---")
    room = ChatRoom()
    inbox: dict[str, list[str]] = {name: [] for name in ("alice", "bob", "carol")}
    for name, messages in inbox.items():
        room.join(name, lambda sender, text, messages=messages: messages.append(f"{sender}: {text}"))
    room.block("carol", "bob")
    print(f"alice broadcasts -> delivered to {room.say('alice', 'hello all')}")
    print(f"bob broadcasts   -> delivered to {room.say('bob', 'hi')} (carol blocked bob)")
    print(f"bob to alice     -> delivered to {room.say('bob', 'psst', to='alice')}")
    for name, messages in inbox.items():
        print(f"  {name:<6} {messages}")
    try:
        room.say("dave", "anyone?")
    except NotFoundError as exc:
        print(f"rejected: {exc}")


if __name__ == "__main__":
    main()
