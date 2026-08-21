"""The simulation clock, the displays and the controller that mediates the cars.

Two levels of lock live here and they are always taken in this order:
``ElevatorController._lock`` first, then ``Elevator._lock``. A car never calls
back into the controller, so that order cannot invert into a deadlock.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from common import FakeClock, IdGenerator, SequentialIdGenerator
from lld.elevator_system.car import Elevator
from lld.elevator_system.models import (
    CabinRequest,
    CapacityExceededError,
    CarStatus,
    CarUnavailableError,
    Direction,
    Floor,
    FloorOutOfRangeError,
    HallRequest,
    NoCarAvailableError,
    Request,
    ServedCall,
)
from lld.elevator_system.strategies import DispatchStrategy, LookDispatch


# --8<-- [start:clock]
class SimulationClock:
    """A discrete clock: one ``tick`` is one second of simulated time.

    It satisfies the shared ``Clock`` protocol, so services take it exactly where
    they would take ``SystemClock``; nothing in this package sleeps or reads the
    wall clock, which is what makes a 400-tick simulation run in milliseconds.
    """

    def __init__(self, seconds_per_tick: float = 1.0, start: float = 0.0) -> None:
        if seconds_per_tick <= 0:
            raise ValueError("seconds_per_tick must be positive")
        self._clock = FakeClock(start)
        self._seconds = seconds_per_tick
        self._ticks = 0
        self._lock = threading.Lock()

    def now(self) -> float:
        return self._clock.now()

    def now_dt(self) -> datetime:
        return self._clock.now_dt()

    @property
    def ticks(self) -> int:
        with self._lock:
            return self._ticks

    def tick(self) -> int:
        """Advance one tick and return the new tick number."""
        with self._lock:
            self._ticks += 1
            count = self._ticks
        self._clock.advance(self._seconds)
        return count


# --8<-- [end:clock]


# --8<-- [start:observer]
class CarListener(Protocol):
    """Observer: anything that wants to be told a car's new status after a tick."""

    def on_car_changed(self, status: CarStatus) -> None: ...


class Display:
    """The indicator above the doors: floor number and direction arrow, per car."""

    def __init__(self, name: str = "lobby") -> None:
        self.name = name
        self._lock = threading.Lock()
        self._by_car: dict[str, CarStatus] = {}

    def on_car_changed(self, status: CarStatus) -> None:
        with self._lock:
            self._by_car[status.car_id] = status

    def floor_of(self, car_id: str) -> int:
        with self._lock:
            return self._by_car[car_id].floor

    def render(self) -> str:
        with self._lock:
            return " | ".join(self._by_car[car_id].render() for car_id in sorted(self._by_car))


# --8<-- [end:observer]


@dataclass(frozen=True, slots=True)
class AssignedCall:
    """Which car owes which hall call: the state of the conversation, owned by the mediator."""

    request: HallRequest
    car_id: str


# --8<-- [start:controller]
class ElevatorController:
    """Mediator: the only object that knows both the landings and the cars.

    A landing never addresses a car and a car never addresses a landing. The
    controller owns the assignment map, the hall lamps, the event log and the
    served-call statistics, all under ``_lock``.
    """

    def __init__(
        self,
        cars: Iterable[Elevator],
        floors: int,
        strategy: DispatchStrategy | None = None,
        clock: SimulationClock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._cars = {car.id: car for car in cars}
        if not self._cars:
            raise ValueError("a controller needs at least one car")
        self.floors = floors
        self._landings = {number: Floor(number) for number in range(floors)}
        self._strategy = strategy or LookDispatch(shaft_height=floors - 1)
        self._clock = clock or SimulationClock()
        self._ids = ids or SequentialIdGenerator("R")
        self._assignments: dict[tuple[int, Direction], AssignedCall] = {}
        self._riders: dict[str, dict[int, int]] = {car_id: {} for car_id in self._cars}
        self._deferred: list[HallRequest] = []
        self._listeners: list[CarListener] = []
        self._served: list[ServedCall] = []
        self._log: list[str] = []
        self._lock = threading.Lock()

    # -- the invoker side of Command -------------------------------------------------
    def submit(self, request: Request) -> str:
        """Log the press, then let the command choose which controller method runs."""
        with self._lock:
            self._log.append(f"t{self._clock.ticks} {self._ids.next_id()} {request.label()}")
        return request.apply(self)

    def hall_call(
        self, floor: int, direction: Direction, destination: int | None = None
    ) -> str:
        return self.submit(HallRequest(floor, direction, self._clock.now(), destination))

    def cabin_request(self, car_id: str, floor: int) -> str:
        return self.submit(CabinRequest(car_id, floor, self._clock.now()))

    # -- the receiver side -----------------------------------------------------------
    def assign_hall_call(self, request: HallRequest) -> str:
        self._check_floor(request.floor)
        with self._lock:
            return self._assign_locked(request)

    def assign_cabin_call(self, request: CabinRequest) -> str:
        self._check_floor(request.floor)
        with self._lock:
            self._car(request.car_id).add_stop(request.floor)
            return request.car_id

    # -- the tick --------------------------------------------------------------------
    def tick(self) -> int:
        """Advance time, step every car, clear the lamps of the calls just answered."""
        statuses: list[CarStatus] = []
        with self._lock:
            self._clock.tick()
            deferred, self._deferred = self._deferred, []
            for request in deferred:
                self._try_assign(request)
            for car in self._cars.values():
                opened = car.step()
                status = car.status()
                if opened:
                    self._on_arrival(status)
                statuses.append(status)
        self._notify(statuses)  # outside the lock: a slow display never stalls the bank
        return self._clock.ticks

    def run(self, ticks: int) -> None:
        for _ in range(ticks):
            self.tick()

    # -- maintenance -----------------------------------------------------------------
    def emergency_stop(self, car_id: str) -> list[str]:
        """Take a car out of service and re-dispatch the hall calls it still owed."""
        with self._lock:
            car = self._car(car_id)
            dropped = car.emergency_stop()
            orphans = [a.request for a in self._assignments.values() if a.car_id == car_id]
            for request in orphans:
                del self._assignments[request.key()]
                self._landings[request.floor].clear(request.direction)
            self._riders[car_id].clear()
            self._log.append(
                f"t{self._clock.ticks} emergency stop {car_id}: dropped {dropped}, "
                f"re-dispatching {len(orphans)} hall call(s)"
            )
            return [car for car in map(self._try_assign, orphans) if car is not None]

    def return_to_service(self, car_id: str) -> None:
        with self._lock:
            self._car(car_id).return_to_service()
            self._log.append(f"t{self._clock.ticks} {car_id} back in service")

    # -- reads -------------------------------------------------------------------------
    def subscribe(self, listener: CarListener) -> None:
        with self._lock:
            self._listeners.append(listener)
            statuses = [car.status() for car in self._cars.values()]
        for status in statuses:
            listener.on_car_changed(status)

    def statuses(self) -> list[CarStatus]:
        with self._lock:
            return [car.status() for car in self._cars.values()]

    def waiting_calls(self) -> list[tuple[int, Direction]]:
        with self._lock:
            return sorted(self._assignments, key=lambda key: (key[0], key[1].value))

    def served_calls(self) -> list[ServedCall]:
        with self._lock:
            return list(self._served)

    def average_wait(self) -> float:
        served = self.served_calls()
        return sum(call.wait() for call in served) / len(served) if served else 0.0

    def total_travel(self) -> int:
        """Floors moved by the whole bank: the energy cost a scheduling policy trades against."""
        with self._lock:
            return sum(car.floors_travelled() for car in self._cars.values())

    def event_log(self) -> list[str]:
        with self._lock:
            return list(self._log)

    # -- helpers: called with ``_lock`` already held -------------------------------------
    def _assign_locked(self, request: HallRequest) -> str:
        landing = self._landings[request.floor]
        assigned = self._assignments.get(request.key())
        if assigned is not None:
            landing.press(request.direction)  # already lit: a second press is absorbed here
            return assigned.car_id
        snapshots = [car.status() for car in self._cars.values()]
        car_id = self._strategy.select(snapshots, request)
        if car_id is None:
            raise NoCarAvailableError(f"no car can answer {request.label()}")
        landing.press(request.direction)
        self._assignments[request.key()] = AssignedCall(request, car_id)
        self._cars[car_id].add_stop(request.floor, request.direction)
        self._log.append(f"t{self._clock.ticks} {request.label()} -> {car_id}")
        return car_id

    def _try_assign(self, request: HallRequest) -> str | None:
        """Re-dispatch after a full car or an emergency stop; defer when nobody can take it."""
        try:
            return self._assign_locked(request)
        except NoCarAvailableError:
            self._deferred.append(request)
            return None

    def _on_arrival(self, status: CarStatus) -> None:
        """Doors just opened: riders get out, waiting passengers get in and press a floor."""
        car = self._cars[status.car_id]
        leaving = self._riders[car.id].pop(status.floor, 0)
        if leaving:
            car.leave(leaving)
        now = self._clock.now()
        for direction in (Direction.UP, Direction.DOWN):
            key = (status.floor, direction)
            assigned = self._assignments.get(key)
            if assigned is None or assigned.car_id != status.car_id:
                continue
            request = assigned.request
            del self._assignments[key]
            self._landings[status.floor].clear(direction)
            if request.destination is not None:
                try:
                    car.board()
                except CapacityExceededError:
                    self._log.append(f"t{self._clock.ticks} {car.id} is full at {status.floor}")
                    self._try_assign(request)
                    continue
                car.add_stop(request.destination)
                riders = self._riders[car.id]
                riders[request.destination] = riders.get(request.destination, 0) + 1
            self._served.append(
                ServedCall(
                    floor=status.floor,
                    direction=direction,
                    car_id=status.car_id,
                    requested_at=request.requested_at,
                    served_at=now,
                )
            )
            self._log.append(
                f"t{self._clock.ticks} {status.car_id} opened at {status.floor}, "
                f"cleared the {direction} lamp"
            )

    def _notify(self, statuses: Sequence[CarStatus]) -> None:
        for listener in list(self._listeners):
            for status in statuses:
                listener.on_car_changed(status)

    def _car(self, car_id: str) -> Elevator:
        try:
            return self._cars[car_id]
        except KeyError:
            raise CarUnavailableError(f"unknown car {car_id}") from None

    def _check_floor(self, floor: int) -> None:
        if not 0 <= floor < self.floors:
            raise FloorOutOfRangeError(f"floor {floor} is outside 0..{self.floors - 1}")


# --8<-- [end:controller]
