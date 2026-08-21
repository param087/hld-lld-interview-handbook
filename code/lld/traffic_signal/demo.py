"""One four-approach intersection: adaptive timing, an ambulance, a pedestrian call."""

from common import FakeClock
from lld.traffic_signal.models import Direction, PedestrianState, Phase, PhaseCycle, SignalState
from lld.traffic_signal.services import IntersectionController, LoopSensor, PedestrianSignal
from lld.traffic_signal.strategies import AdaptiveTiming

N, S, E, W = Direction.NORTH, Direction.SOUTH, Direction.EAST, Direction.WEST
CYCLE = PhaseCycle(
    (
        Phase("NS", frozenset({N, S}), min_green=6, max_green=16),
        Phase("EW", frozenset({E, W}), min_green=6, max_green=16),
    )
)
ARRIVALS = (N, S, N, N, S, N, S, N)  # only the north-south road is loading up
LETTERS = {
    SignalState.RED: "R",
    SignalState.GREEN: "G",
    SignalState.YELLOW: "Y",
    SignalState.FLASHING_RED: "F",
}


def lights(controller: IntersectionController) -> str:
    return " ".join(
        f"{d.value[0].upper()}:{LETTERS[controller.light(d).status()]}" for d in (N, S, E, W)
    )


def queues(sensor: LoopSensor) -> str:
    counts = sensor.snapshot()
    return "/".join(str(counts.get(d, 0)) for d in (N, S, E, W))


def run(
    controller: IntersectionController,
    sensor: LoopSensor,
    crossing: PedestrianSignal,
    ticks: int,
) -> None:
    """Tick the intersection, move some traffic, print only when the stage changes."""
    before = (controller.stage(), controller.current_phase())
    for _ in range(ticks):
        tick = controller.tick()
        sensor.arrive(ARRIVALS[tick % len(ARRIVALS)])
        for direction in controller.green_directions():
            sensor.depart(direction, 3)
        now = (controller.stage(), controller.current_phase())
        if now == before:
            continue
        before = now
        walk = "walk" if crossing.status() is PedestrianState.WALK else "wait"
        print(
            f"t{tick:>3} {now[0]:<9} {now[1]:<3} {lights(controller)}"
            f"  west ped {walk:<4} queues {queues(sensor)}"
        )


def main() -> None:
    sensor = LoopSensor({N: 6, S: 3, E: 9, W: 2})
    controller = IntersectionController(
        CYCLE,
        sensor=sensor,
        timing=AdaptiveTiming(base=6, per_tick=2),
        clock=FakeClock(1_700_000_000),
        yellow_ticks=3,
        all_red_ticks=2,
        walk_ticks=8,
    )
    crossing = PedestrianSignal(W)
    controller.subscribe(crossing)
    print("--- two phases, adaptive green, queues shown as N/S/E/W ---")
    run(controller, sensor, crossing, 8)

    controller.request_emergency(N)
    print("ambulance from the north: the east-west green is cut short, then held for it")
    run(controller, sensor, crossing, 14)
    controller.clear_emergency()
    print("ambulance clear, the ring resumes through yellow and all-red")
    run(controller, sensor, crossing, 10)

    controller.pedestrian_call(W)
    print("west button pressed, and no car has waited there for twenty ticks")
    run(controller, sensor, crossing, 14)

    controller.enter_maintenance()
    print(f"maintenance {lights(controller)}  stage {controller.stage()}")
    for line in controller.event_log()[-3:]:
        print(line)


if __name__ == "__main__":
    main()
