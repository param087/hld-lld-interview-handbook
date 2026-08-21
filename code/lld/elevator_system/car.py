"""One elevator car: its state machine, its two stop sets and its own lock.

The car is the only object that mutates its floor, direction, door and stops,
and every public method here takes ``_lock``; the private helpers assume it is
already held. It never calls the controller - it only returns values - which is
what makes the controller-then-car lock order safe.
"""

from __future__ import annotations

import threading

from lld.elevator_system.models import (
    CapacityExceededError,
    CarStatus,
    CarUnavailableError,
    Direction,
    Door,
    DoorState,
    ElevatorState,
    FloorOutOfRangeError,
)


# --8<-- [start:car]
class Elevator:
    """One car: its own lock, its own two stop sets, its own state machine.

    Stops are split by the direction in which they will be served, which is what
    makes LOOK a three-line rule instead of a scheduler: serve everything in the
    current direction, then turn.
    """

    def __init__(
        self,
        car_id: str,
        floors: int,
        capacity: int = 8,
        start_floor: int = 0,
        door_hold: int = 2,
    ) -> None:
        if floors < 2:
            raise ValueError("a shaft needs at least two floors")
        self.id = car_id
        self.floors = floors
        self.capacity = capacity
        self.floor = start_floor
        self.state = ElevatorState.IDLE
        self.direction = Direction.IDLE
        self.load = 0
        self.travelled = 0  # floors moved so far: the energy half of the strategy bake-off
        self.door = Door(default_hold=door_hold)
        self._up_stops: set[int] = set()
        self._down_stops: set[int] = set()
        self._opened = False  # set by _open_door, read and reset by step
        self._lock = threading.Lock()

    # -- commands ------------------------------------------------------------------
    def add_stop(self, floor: int, direction: Direction | None = None) -> None:
        """Queue a stop. ``direction`` is the *call* direction for a hall request."""
        if not 0 <= floor < self.floors:
            raise FloorOutOfRangeError(f"floor {floor} is outside 0..{self.floors - 1}")
        with self._lock:
            if self.state is ElevatorState.MAINTENANCE:
                raise CarUnavailableError(f"car {self.id} is in maintenance")
            if direction is Direction.UP:
                self._up_stops.add(floor)
            elif direction is Direction.DOWN:
                self._down_stops.add(floor)
            elif floor >= self.floor:
                self._up_stops.add(floor)
            else:
                self._down_stops.add(floor)

    def board(self, passengers: int = 1) -> None:
        with self._lock:
            if not self.door.is_open():
                raise CarUnavailableError(f"car {self.id} has its doors closed")
            if self.load + passengers > self.capacity:
                raise CapacityExceededError(
                    f"car {self.id} holds {self.capacity}; {self.load} + {passengers} does not fit"
                )
            self.load += passengers

    def leave(self, passengers: int = 1) -> None:
        with self._lock:
            self.load = max(0, self.load - passengers)

    def obstruct_door(self) -> None:
        with self._lock:
            self.door.obstruct()

    def clear_door(self) -> None:
        with self._lock:
            self.door.clear_obstruction()

    def emergency_stop(self) -> list[int]:
        """Drop every stop, open up where we are, go out of service. Returns the dropped floors."""
        with self._lock:
            dropped = sorted(self._up_stops | self._down_stops)
            self._up_stops.clear()
            self._down_stops.clear()
            self.door.open()
            self.direction = Direction.IDLE
            self.state = ElevatorState.MAINTENANCE
            return dropped

    def return_to_service(self) -> None:
        with self._lock:
            if self.state is not ElevatorState.MAINTENANCE:
                raise CarUnavailableError(f"car {self.id} is not in maintenance")
            self.door.state = DoorState.CLOSED
            self.door.hold_ticks = 0
            self.state = ElevatorState.IDLE

    # -- the tick ------------------------------------------------------------------
    def step(self) -> bool:
        """One tick: run the door timer, or move one floor, then re-decide.

        Returns True if the doors opened during this tick. A car standing with
        its doors open can be given a stop at its own floor, close and reopen in
        the same tick, so the controller cannot detect an arrival by watching the
        status change; the car reports the event instead.
        """
        with self._lock:
            self._opened = False
            if self.state is ElevatorState.MAINTENANCE:
                return False
            if self.state is ElevatorState.DOOR_OPEN:
                if self.door.tick():
                    self._resume()
                return self._opened
            if self.state is ElevatorState.MOVING_UP:
                self.floor += 1
                self.travelled += 1
            elif self.state is ElevatorState.MOVING_DOWN:
                self.floor -= 1
                self.travelled += 1
            self._resume()
            return self._opened

    def floors_travelled(self) -> int:
        with self._lock:
            return self.travelled

    def status(self) -> CarStatus:
        with self._lock:
            return CarStatus(
                car_id=self.id,
                floor=self.floor,
                direction=self.direction,
                state=self.state,
                door=self.door.state,
                stops=tuple(sorted(self._up_stops | self._down_stops)),
                load=self.load,
                capacity=self.capacity,
            )

    # -- helpers: called with the lock already held --------------------------------
    def _resume(self) -> None:
        if self._stop_here():
            self._open_door()
            return
        self.direction = self._next_direction()
        self.state = {
            Direction.UP: ElevatorState.MOVING_UP,
            Direction.DOWN: ElevatorState.MOVING_DOWN,
            Direction.IDLE: ElevatorState.IDLE,
        }[self.direction]

    def _open_door(self) -> None:
        self._up_stops.discard(self.floor)
        self._down_stops.discard(self.floor)
        self.door.open()
        self.state = ElevatorState.DOOR_OPEN
        self._opened = True

    def _stop_here(self) -> bool:
        if self.direction is Direction.UP:
            return self.floor in self._up_stops or (
                self.floor in self._down_stops and not self._work_above()
            )
        if self.direction is Direction.DOWN:
            return self.floor in self._down_stops or (
                self.floor in self._up_stops and not self._work_below()
            )
        return self.floor in self._up_stops or self.floor in self._down_stops

    def _next_direction(self) -> Direction:
        if self.direction is Direction.UP and self._work_above():
            return Direction.UP
        if self.direction is Direction.DOWN and self._work_below():
            return Direction.DOWN
        if self._work_above():
            return Direction.UP
        if self._work_below():
            return Direction.DOWN
        return Direction.IDLE

    def _work_above(self) -> bool:
        return any(f > self.floor for f in self._up_stops | self._down_stops)

    def _work_below(self) -> bool:
        return any(f < self.floor for f in self._up_stops | self._down_stops)


# --8<-- [end:car]
