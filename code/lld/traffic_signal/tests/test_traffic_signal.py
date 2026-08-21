from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock
from lld.traffic_signal.models import (
    ConflictingMovementError,
    ControllerState,
    Direction,
    PedestrianState,
    Phase,
    PhaseCycle,
    PhaseDemand,
    Sensor,
    SignalState,
    SignalStateError,
    UnknownApproachError,
)
from lld.traffic_signal.services import (
    IntersectionController,
    LoopSensor,
    PedestrianSignal,
    TrafficLight,
)
from lld.traffic_signal.strategies import AdaptiveTiming, FixedTiming, TimingStrategy

N, S, E, W = Direction.NORTH, Direction.SOUTH, Direction.EAST, Direction.WEST
NS = Phase("NS", frozenset({N, S}), min_green=3, max_green=20)
EW = Phase("EW", frozenset({E, W}), min_green=3, max_green=20)
CYCLE = PhaseCycle((NS, EW))


def build(
    sensor: Sensor | None = None,
    timing: TimingStrategy | None = None,
    walk_ticks: int = 4,
) -> IntersectionController:
    return IntersectionController(
        CYCLE,
        sensor=sensor,
        timing=timing or FixedTiming(5),
        clock=FakeClock(1000.0),
        yellow_ticks=2,
        all_red_ticks=2,
        walk_ticks=walk_ticks,
    )


def stages(controller: IntersectionController, ticks: int) -> list[ControllerState]:
    seen = []
    for _ in range(ticks):
        controller.tick()
        seen.append(controller.stage())
    return seen


def test_a_phase_whose_movements_cross_cannot_be_built() -> None:
    with pytest.raises(ConflictingMovementError):
        Phase("bad", frozenset({N, E}))
    Phase("fine", frozenset({N, S}))  # opposing through movements are compatible


# --8<-- [start:cycle]
def test_the_ring_runs_green_yellow_all_red_then_the_next_phase() -> None:
    controller = build()
    assert stages(controller, 12) == [
        ControllerState.ALL_RED,  # the intersection starts dark-safe
        *[ControllerState.GREEN] * 5,  # five ticks of green, as FixedTiming asked
        *[ControllerState.YELLOW] * 2,
        *[ControllerState.ALL_RED] * 2,  # mandatory clearance between two greens
        *[ControllerState.GREEN] * 2,
    ]
    assert controller.current_phase() == "NS"  # the ring moved on


# --8<-- [end:cycle]


# --8<-- [start:safety]
def test_conflicting_approaches_are_never_green_and_every_change_clears_first() -> None:
    controller = build(sensor=LoopSensor({N: 4, S: 2, E: 6, W: 3}), timing=AdaptiveTiming())
    seen: list[frozenset[Direction]] = []
    for tick in range(1, 61):
        if tick == 12:
            controller.pedestrian_call(W)
        if tick == 20:
            controller.request_emergency(E)
        if tick == 30:
            controller.clear_emergency()
        controller.tick()
        green = controller.green_directions()
        assert not any(one.conflicts_with(other) for one in green for other in green)
        seen.append(green)

    changes = [g for i, g in enumerate(seen) if i == 0 or g != seen[i - 1]]
    for index, green in enumerate(changes):
        if green and index:
            assert not changes[index - 1], "a green set must be preceded by an all-red"


# --8<-- [end:safety]


def test_an_emergency_never_shortens_a_yellow() -> None:
    controller = build()
    controller.run(7)  # green expires on tick 7, so the intersection is now yellow
    assert controller.stage() is ControllerState.YELLOW
    controller.request_emergency(N)
    assert stages(controller, 4) == [
        ControllerState.YELLOW,  # the yellow runs its full length
        ControllerState.ALL_RED,
        ControllerState.ALL_RED,  # and so does the clearance
        ControllerState.EMERGENCY,
    ]
    assert controller.green_directions() == frozenset({N, S})


def test_an_emergency_for_an_approach_already_green_holds_it_without_a_gap() -> None:
    controller = build()
    controller.run(3)
    assert controller.green_directions() == frozenset({E, W})
    controller.request_emergency(E)
    controller.tick()
    assert controller.stage() is ControllerState.EMERGENCY
    assert controller.light(E).status() is SignalState.GREEN  # never went yellow
    controller.run(30)
    assert controller.stage() is ControllerState.EMERGENCY  # held, not timed out
    controller.clear_emergency()
    controller.tick()
    assert controller.stage() is ControllerState.YELLOW


def test_a_pedestrian_call_stops_a_quiet_phase_from_being_skipped() -> None:
    sensor = LoopSensor({N: 6, S: 6, E: 0, W: 0})
    controller = build(sensor=sensor, timing=AdaptiveTiming(base=3, per_tick=2), walk_ticks=7)
    crossing = PedestrianSignal(W)
    controller.subscribe(crossing)
    served = set()
    for _ in range(40):
        controller.tick()
        if controller.green_directions():
            served.add(controller.current_phase())
    assert served == {"NS"}  # EW skipped every time: nobody is waiting there

    controller.pedestrian_call(W)
    for _ in range(40):
        controller.tick()
        if crossing.status() is PedestrianState.WALK:
            break
    assert crossing.status() is PedestrianState.WALK
    assert controller.pending_pedestrians() == set()  # the call was consumed
    walked = 0
    while crossing.status() is PedestrianState.WALK:
        controller.tick()
        walked += 1
    assert walked >= 7  # the green was stretched to the walk interval


@pytest.mark.parametrize(("waiting", "expected"), [(0, 6), (8, 10), (100, 20)])
def test_adaptive_green_grows_with_the_queue_and_stops_at_the_cap(
    waiting: int, expected: int
) -> None:
    demand = PhaseDemand(NS, waiting, pedestrian_waiting=False)
    assert NS.clamp(AdaptiveTiming(base=6, per_tick=2).green_ticks(demand)) == expected


def test_maintenance_flashes_red_and_resumes_through_a_clearance() -> None:
    controller = build()
    controller.run(4)
    controller.enter_maintenance()
    assert all(controller.light(d).status() is SignalState.FLASHING_RED for d in (N, S, E, W))
    controller.run(6)  # ticks do nothing while the cabinet is open
    assert controller.stage() is ControllerState.MAINTENANCE
    controller.leave_maintenance()
    assert controller.stage() is ControllerState.ALL_RED
    with pytest.raises(SignalStateError):
        controller.leave_maintenance()


def test_an_approach_this_intersection_does_not_serve_is_rejected() -> None:
    controller = IntersectionController(
        PhaseCycle((NS,)), clock=FakeClock(0.0), yellow_ticks=2, all_red_ticks=2, walk_ticks=4
    )
    with pytest.raises(UnknownApproachError):
        controller.request_emergency(E)
    with pytest.raises(UnknownApproachError):
        controller.pedestrian_call(E)
    controller.run(10)
    assert controller.stage() is not ControllerState.MAINTENANCE  # the ring kept running


# --8<-- [start:concurrency]
def test_presses_from_many_threads_never_break_the_safety_invariant() -> None:
    controller = build(sensor=LoopSensor({N: 5, S: 5, E: 5, W: 5}), timing=AdaptiveTiming())
    violations: list[frozenset[Direction]] = []
    presses = 24

    def press(index: int) -> None:
        if index % 3 == 0:
            controller.pedestrian_call(W)
        elif index % 3 == 1:
            controller.request_emergency(N if index % 2 else E)
        else:
            controller.clear_emergency()

    def drive(_: int) -> None:
        for _ in range(40):
            controller.tick()
            green = controller.green_directions()
            if any(one.conflicts_with(other) for one in green for other in green):
                violations.append(green)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(drive, 0)]
        futures += [pool.submit(press, index) for index in range(presses)]
        for future in futures:
            future.result()

    assert violations == []
    log = controller.event_log()
    assert sum(1 for line in log if "queued:" in line) == presses  # no press was lost


# --8<-- [end:concurrency]


def test_the_heads_are_observers_and_are_never_polled() -> None:
    controller = build()
    assert controller.light(E).status() is SignalState.RED
    controller.run(3)
    assert controller.light(E).status() is SignalState.GREEN
    assert controller.light(N).status() is SignalState.RED
    head = TrafficLight(N)
    controller.subscribe(head)  # a head added later is told the current state at once
    assert head.status() is SignalState.RED
