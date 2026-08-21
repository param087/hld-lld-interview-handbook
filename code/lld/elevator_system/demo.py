"""Three cars in a twelve-floor building, then the same workload under four strategies."""

from lld.elevator_system.car import Elevator
from lld.elevator_system.models import Direction
from lld.elevator_system.services import Display, ElevatorController, SimulationClock
from lld.elevator_system.strategies import (
    DestinationDispatch,
    DispatchStrategy,
    FcfsDispatch,
    LookDispatch,
    NearestCarDispatch,
)

FLOORS = 12
START_FLOORS = {"A": 0, "B": 5, "C": 11}
# (tick, floor, call direction, destination the passenger declares at the landing)
WORKLOAD: tuple[tuple[int, int, Direction, int], ...] = (
    (1, 6, Direction.DOWN, 3),
    (2, 0, Direction.UP, 11),
    (6, 1, Direction.UP, 3),
    (6, 4, Direction.DOWN, 1),
    (6, 5, Direction.UP, 11),
    (6, 10, Direction.DOWN, 8),
    (7, 6, Direction.UP, 10),
    (7, 10, Direction.UP, 11),
)


def build(strategy: DispatchStrategy) -> tuple[ElevatorController, Display]:
    cars = [Elevator(name, FLOORS, start_floor=floor) for name, floor in START_FLOORS.items()]
    controller = ElevatorController(cars, FLOORS, strategy=strategy, clock=SimulationClock())
    display = Display("lobby")
    controller.subscribe(display)
    return controller, display


def waiting(controller: ElevatorController) -> str:
    calls = controller.waiting_calls()
    return ", ".join(f"{floor}{direction.arrow()}" for floor, direction in calls) or "none"


def show(controller: ElevatorController, display: Display, ticks: int = 1) -> None:
    for _ in range(ticks):
        tick = controller.tick()
        print(f"t{tick} {display.render()}   waiting: {waiting(controller)}")


def replay(strategy: DispatchStrategy, ticks: int = 90) -> tuple[float, int, int]:
    """Run the fixed workload against a fresh building; report wait, calls served, floors moved."""
    controller, _ = build(strategy)
    for tick in range(ticks):
        for at, floor, direction, destination in WORKLOAD:
            if at == tick:
                controller.hall_call(floor, direction, destination)
        controller.tick()
    return controller.average_wait(), len(controller.served_calls()), controller.total_travel()


def main() -> None:
    controller, display = build(LookDispatch(shaft_height=FLOORS - 1))
    print("--- three cars, twelve floors, LOOK dispatch ---")
    print(f"t0 {display.render()}   waiting: {waiting(controller)}")
    print(f"hall call 7 up (to 11) -> car {controller.hall_call(7, Direction.UP, 11)}")
    show(controller, display, ticks=2)
    print(f"hall call 1 up (to 9)  -> car {controller.hall_call(1, Direction.UP, 9)}")
    show(controller, display)
    print(f"emergency stop A -> its hall calls move to {controller.emergency_stop('A')}")
    show(controller, display, ticks=2)
    for call in controller.served_calls():
        print(f"served {call.floor}{call.direction.arrow()} by {call.car_id} after {call.wait():.0f} ticks")

    print("--- eight calls replayed against each strategy ---")
    look = LookDispatch(shaft_height=FLOORS - 1)
    for strategy in (FcfsDispatch(), NearestCarDispatch(), look, DestinationDispatch(look)):
        wait, served, travel = replay(strategy)
        print(f"{strategy.name:<12} wait {wait:>5.2f} ticks   {travel:>3} floors travelled   {served}/8 served")


if __name__ == "__main__":
    main()
