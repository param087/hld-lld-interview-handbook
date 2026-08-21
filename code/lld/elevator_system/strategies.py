"""Dispatch policies (Strategy): which car answers a hall call.

Every strategy is a pure function of immutable ``CarStatus`` snapshots, so it
never touches a car's lock and can be unit-tested without building a building.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from lld.elevator_system.models import CarStatus, Direction, HallRequest


# --8<-- [start:protocol]
class DispatchStrategy(Protocol):
    """Scores the cars and names one. Returns None when no car can take the call."""

    name: str

    def select(self, cars: Sequence[CarStatus], request: HallRequest) -> str | None: ...


def _eligible(cars: Sequence[CarStatus]) -> list[CarStatus]:
    return [car for car in cars if car.in_service() and car.has_room()]


# --8<-- [end:protocol]


# --8<-- [start:simple]
class FcfsDispatch:
    """First come, first served: the first idle car, else the shortest queue.

    The baseline you implement in two minutes and then criticise: it ignores
    where the car is and which way it is going, so a call one floor away can
    wait for a car at the other end of the shaft.
    """

    name = "fcfs"

    def select(self, cars: Sequence[CarStatus], request: HallRequest) -> str | None:
        eligible = _eligible(cars)
        if not eligible:
            return None
        idle = [car for car in eligible if car.is_idle()]
        if idle:
            return idle[0].car_id
        return min(eligible, key=lambda car: (len(car.stops), car.car_id)).car_id


class NearestCarDispatch:
    """Fewest floors away, idle cars winning ties.

    Better than FCFS and still wrong in one obvious way: a car one floor away
    that is speeding past in the other direction scores better than an idle car
    three floors away, and the passenger watches it go by.
    """

    name = "nearest_car"

    def select(self, cars: Sequence[CarStatus], request: HallRequest) -> str | None:
        eligible = _eligible(cars)
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda car: (car.distance_to(request.floor), not car.is_idle(), car.car_id),
        ).car_id


# --8<-- [end:simple]


# --8<-- [start:look]
class LookDispatch:
    """SCAN/LOOK: a car keeps going while stops remain ahead, then turns.

    The cost of a call is the number of floors the car must travel before it can
    open its doors there, so a car heading your way is cheap, a car that must
    finish its run first pays one shaft, and a car that has already passed you
    pays two.
    """

    name = "look"

    def __init__(self, shaft_height: int) -> None:
        if shaft_height <= 0:
            raise ValueError("shaft_height must be positive")
        self._shaft = shaft_height

    def cost(self, car: CarStatus, request: HallRequest) -> int:
        distance = car.distance_to(request.floor)
        if car.is_idle():
            return distance
        going_up = car.direction is Direction.UP
        ahead = request.floor >= car.floor if going_up else request.floor <= car.floor
        if ahead and car.direction is request.direction:
            return distance  # picked up on the way, no turn needed
        if ahead:
            return distance + self._shaft  # served after the car turns at the end
        return distance + 2 * self._shaft  # the car has already passed this floor

    def select(self, cars: Sequence[CarStatus], request: HallRequest) -> str | None:
        eligible = _eligible(cars)
        if not eligible:
            return None
        return min(
            eligible, key=lambda car: (self.cost(car, request), len(car.stops), car.car_id)
        ).car_id


class DestinationDispatch:
    """Ask for the destination at the landing, then group passengers by it.

    When the lobby panel knows you want floor 9, a car already stopping at 9 and
    still able to pick you up costs nothing extra; only when no car matches does
    this fall back to the wrapped strategy.
    """

    name = "destination"

    def __init__(self, fallback: DispatchStrategy) -> None:
        self._fallback = fallback

    def select(self, cars: Sequence[CarStatus], request: HallRequest) -> str | None:
        if request.destination is None:
            return self._fallback.select(cars, request)
        grouped = [
            car
            for car in _eligible(cars)
            if request.destination in car.stops and self._on_the_way(car, request)
        ]
        if not grouped:
            return self._fallback.select(cars, request)
        return min(grouped, key=lambda car: (car.distance_to(request.floor), car.car_id)).car_id

    @staticmethod
    def _on_the_way(car: CarStatus, request: HallRequest) -> bool:
        if car.is_idle():
            return True
        if car.direction is Direction.UP:
            return car.floor <= request.floor
        return car.floor >= request.floor


# --8<-- [end:look]
