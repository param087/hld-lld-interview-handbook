from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import pytest

from lld.elevator_system.car import Elevator
from lld.elevator_system.models import (
    CapacityExceededError,
    CarStatus,
    CarUnavailableError,
    Direction,
    DoorObstructedError,
    DoorState,
    ElevatorState,
    FloorOutOfRangeError,
    HallRequest,
    NoCarAvailableError,
)
from lld.elevator_system.services import Display, ElevatorController, SimulationClock
from lld.elevator_system.strategies import (
    DestinationDispatch,
    DispatchStrategy,
    FcfsDispatch,
    LookDispatch,
    NearestCarDispatch,
)


def build(
    starts: dict[str, int],
    floors: int = 12,
    capacity: int = 8,
    strategy: DispatchStrategy | None = None,
) -> ElevatorController:
    cars = [Elevator(name, floors, capacity=capacity, start_floor=at) for name, at in starts.items()]
    return ElevatorController(
        cars,
        floors,
        strategy=strategy or LookDispatch(shaft_height=floors - 1),
        clock=SimulationClock(),
    )


def states_of(controller: ElevatorController, car_id: str, ticks: int) -> list[ElevatorState]:
    seen = []
    for _ in range(ticks):
        controller.tick()
        seen.append(next(s.state for s in controller.statuses() if s.car_id == car_id))
    return seen


def test_hall_call_is_served_and_the_passenger_presses_a_destination() -> None:
    controller = build({"A": 0, "B": 5})
    assert controller.hall_call(7, Direction.UP, destination=11) == "B"
    controller.run(3)  # tick 1 chooses a direction, ticks 2 and 3 move 5 -> 7
    status = next(s for s in controller.statuses() if s.car_id == "B")
    assert (status.floor, status.state, status.load) == (7, ElevatorState.DOOR_OPEN, 1)
    assert status.stops == (11,)  # boarding queued the declared destination
    assert [(c.floor, c.car_id, c.wait()) for c in controller.served_calls()] == [(7, "B", 3.0)]
    assert controller.waiting_calls() == []  # the hall lamp is out


@pytest.mark.parametrize("floor", [-1, 12, 99])
def test_calls_outside_the_building_are_rejected(floor: int) -> None:
    controller = build({"A": 0})
    with pytest.raises(FloorOutOfRangeError):
        controller.hall_call(floor, Direction.UP)


def test_a_car_walks_idle_then_moving_then_door_open_then_idle() -> None:
    controller = build({"A": 0}, floors=4)
    controller.cabin_request("A", 2)
    assert states_of(controller, "A", 6) == [
        ElevatorState.MOVING_UP,  # an idle car spends its first tick choosing a direction
        ElevatorState.MOVING_UP,  # floor 1
        ElevatorState.DOOR_OPEN,  # floor 2 reached, doors open
        ElevatorState.DOOR_OPEN,  # the dwell timer runs
        ElevatorState.IDLE,  # doors closed and nothing left to serve
        ElevatorState.IDLE,
    ]


# --8<-- [start:sweep]
def test_look_finishes_the_upward_sweep_before_it_turns() -> None:
    controller = build({"A": 3}, floors=10)
    for floor in (6, 8, 1):  # the call below arrives while the car is already going up
        controller.cabin_request("A", floor)
    opened_at: list[int] = []
    for _ in range(24):
        controller.tick()
        status = controller.statuses()[0]
        if status.door is DoorState.OPEN and status.floor not in opened_at:
            opened_at.append(status.floor)
    assert opened_at == [6, 8, 1]  # everything above first, then one reversal


# --8<-- [end:sweep]


def test_pressing_the_same_button_twice_is_absorbed() -> None:
    controller = build({"A": 0, "B": 5})
    first = controller.hall_call(9, Direction.DOWN)
    second = controller.hall_call(9, Direction.DOWN)
    assert first == second == "B"
    assert controller.waiting_calls() == [(9, Direction.DOWN)]
    controller.run(20)
    assert len(controller.served_calls()) == 1  # one call, not two


# --8<-- [start:concurrency]
def test_concurrent_presses_assign_every_button_to_exactly_one_car() -> None:
    controller = build({"A": 0, "B": 5, "C": 11}, strategy=NearestCarDispatch())
    buttons = [(floor, Direction.UP) for floor in range(1, 9)]
    presses = [button for button in buttons for _ in range(5)]  # 40 presses, 8 buttons

    def press(button: tuple[int, Direction]) -> str:
        return controller.hall_call(button[0], button[1])

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(press, presses))

    cars_per_button: dict[tuple[int, Direction], set[str]] = defaultdict(set)
    for button, car_id in zip(presses, answers, strict=True):
        cars_per_button[button].add(car_id)
    assert all(len(cars) == 1 for cars in cars_per_button.values())  # never split between cars
    assert len(controller.waiting_calls()) == len(buttons)
    queued = sorted(floor for status in controller.statuses() for floor in status.stops)
    assert queued == [floor for floor, _ in buttons]  # each floor queued in exactly one car


# --8<-- [end:concurrency]


def test_emergency_stop_hands_the_calls_it_owed_to_another_car() -> None:
    controller = build({"A": 0, "B": 5, "C": 11})
    assert controller.hall_call(1, Direction.UP, destination=9) == "A"
    assert controller.emergency_stop("A") == ["B"]
    stopped = next(s for s in controller.statuses() if s.car_id == "A")
    assert stopped.state is ElevatorState.MAINTENANCE and stopped.stops == ()
    assert 1 in next(s for s in controller.statuses() if s.car_id == "B").stops
    with pytest.raises(CarUnavailableError):
        controller.cabin_request("A", 4)
    controller.return_to_service("A")
    controller.cabin_request("A", 4)  # accepted again once the car is back in service


def test_every_car_out_of_service_means_the_call_cannot_be_taken() -> None:
    controller = build({"A": 0, "B": 5})
    controller.emergency_stop("A")
    controller.emergency_stop("B")
    with pytest.raises(NoCarAvailableError):
        controller.hall_call(3, Direction.UP)


def test_an_obstructed_door_holds_the_car_at_its_floor() -> None:
    car = Elevator("A", 6)
    controller = ElevatorController([car], 6, clock=SimulationClock())
    controller.cabin_request("A", 2)
    controller.run(3)  # arrive at floor 2 and open
    assert car.status().door is DoorState.OPEN
    car.obstruct_door()
    controller.cabin_request("A", 5)
    controller.run(5)  # the dwell timer restarts on every obstructed tick
    assert car.status().floor == 2 and car.status().state is ElevatorState.DOOR_OPEN
    car.clear_door()
    controller.run(5)
    assert car.status().floor == 5
    with pytest.raises(DoorObstructedError):
        Elevator("B", 4).obstruct_door()  # a closed door cannot be obstructed


def test_a_full_car_hands_the_waiting_passenger_back_to_the_bank() -> None:
    controller = build({"A": 0}, floors=6, capacity=1)
    controller.hall_call(1, Direction.UP, destination=5)
    controller.hall_call(3, Direction.UP, destination=5)
    controller.run(30)
    assert any("is full at 3" in line for line in controller.event_log())
    assert {call.floor for call in controller.served_calls()} == {1, 3}


def test_boarding_more_than_the_rated_load_is_refused() -> None:
    car = Elevator("A", 4, capacity=2)
    car.add_stop(1)
    car.step()
    car.step()  # arrive at floor 1, doors open
    car.board(2)
    with pytest.raises(CapacityExceededError):
        car.board(1)


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (FcfsDispatch(), "A"),  # the first idle car in the bank, wherever it stands
        (NearestCarDispatch(), "B"),  # two floors away, but travelling the other way
        (LookDispatch(shaft_height=11), "C"),  # idle, four floors away, no turnaround
    ],
)
def test_strategies_pick_different_cars_for_the_same_call(
    strategy: DispatchStrategy, expected: str
) -> None:
    cars = [
        CarStatus("A", 0, Direction.IDLE, ElevatorState.IDLE, DoorState.CLOSED, (), 0, 8),
        CarStatus("B", 8, Direction.UP, ElevatorState.MOVING_UP, DoorState.CLOSED, (11,), 1, 8),
        CarStatus("C", 2, Direction.IDLE, ElevatorState.IDLE, DoorState.CLOSED, (), 0, 8),
    ]
    request = HallRequest(floor=6, direction=Direction.DOWN, requested_at=0.0)
    assert strategy.select(cars, request) == expected


def test_destination_dispatch_groups_passengers_going_to_the_same_floor() -> None:
    look = LookDispatch(shaft_height=11)
    cars = [
        CarStatus("A", 5, Direction.IDLE, ElevatorState.IDLE, DoorState.CLOSED, (), 0, 8),
        CarStatus("B", 2, Direction.UP, ElevatorState.MOVING_UP, DoorState.CLOSED, (11,), 2, 8),
    ]
    request = HallRequest(floor=6, direction=Direction.UP, requested_at=0.0, destination=11)
    assert DestinationDispatch(look).select(cars, request) == "B"  # already stopping at 11
    assert look.select(cars, request) == "A"  # LOOK alone prefers the idle car at 5


def test_the_display_follows_every_car_without_polling() -> None:
    controller = build({"A": 0, "B": 5})
    display = Display("lobby")
    controller.subscribe(display)
    assert display.floor_of("B") == 5
    controller.cabin_request("B", 8)
    controller.run(4)
    assert display.floor_of("B") == 8
    assert "A" in display.render() and "B" in display.render()
