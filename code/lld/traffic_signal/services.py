"""The signal heads, the pedestrian signals, the loops and the controller.

One lock, `IntersectionController._lock`, guards the whole cycle: stage, elapsed
ticks, phase index, the command queue and the pedestrian calls. Everything that
could break the safety invariant moves together under it. The heads have their
own tiny lock because they are read from other threads, and they are notified
outside the controller lock so a slow observer cannot stretch a yellow.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from common import Clock, HandbookError, SystemClock
from lld.traffic_signal.models import (
    ClearOverride,
    ControllerState,
    Direction,
    EmergencyOverride,
    PedestrianCall,
    PedestrianState,
    Phase,
    PhaseCycle,
    PhaseDemand,
    PhaseListener,
    Sensor,
    SignalCommand,
    SignalEvent,
    SignalState,
    SignalStateError,
    SignalUpdate,
)
from lld.traffic_signal.strategies import FixedTiming, TimingStrategy


# --8<-- [start:heads]
class TrafficLight:
    """One signal head. It is an observer: it works out its own colour from the update."""

    def __init__(self, direction: Direction) -> None:
        self.direction = direction
        self._state = SignalState.RED
        self._lock = threading.Lock()

    def on_phase_changed(self, update: SignalUpdate) -> None:
        colour = self._colour_for(update)
        with self._lock:
            self._state = colour

    def status(self) -> SignalState:
        with self._lock:
            return self._state

    def _colour_for(self, update: SignalUpdate) -> SignalState:
        if update.stage is ControllerState.MAINTENANCE:
            return SignalState.FLASHING_RED
        if self.direction not in update.movements:
            return SignalState.RED
        if update.stage in (ControllerState.GREEN, ControllerState.EMERGENCY):
            return SignalState.GREEN
        if update.stage is ControllerState.YELLOW:
            return SignalState.YELLOW
        return SignalState.RED


class PedestrianSignal:
    """Walks with the traffic beside it, and never during an emergency hold."""

    def __init__(self, direction: Direction) -> None:
        self.direction = direction
        self._state = PedestrianState.DONT_WALK
        self._lock = threading.Lock()

    def on_phase_changed(self, update: SignalUpdate) -> None:
        walking = update.stage is ControllerState.GREEN and self.direction in update.movements
        with self._lock:
            self._state = PedestrianState.WALK if walking else PedestrianState.DONT_WALK

    def status(self) -> PedestrianState:
        with self._lock:
            return self._state


class LoopSensor:
    """A stand-in for induction loops: the environment drives it, the controller reads it."""

    def __init__(self, queues: Mapping[Direction, int] | None = None) -> None:
        self._queues: dict[Direction, int] = dict(queues or {})
        self._lock = threading.Lock()

    def waiting(self, direction: Direction) -> int:
        with self._lock:
            return self._queues.get(direction, 0)

    def arrive(self, direction: Direction, count: int = 1) -> None:
        with self._lock:
            self._queues[direction] = self._queues.get(direction, 0) + count

    def depart(self, direction: Direction, count: int = 1) -> None:
        with self._lock:
            self._queues[direction] = max(0, self._queues.get(direction, 0) - count)

    def snapshot(self) -> dict[Direction, int]:
        with self._lock:
            return dict(self._queues)


# --8<-- [end:heads]


# --8<-- [start:controller]
class IntersectionController:
    """Mediator: loops, heads, buttons and the timing policy only ever talk to this.

    The cycle is a tick counter, not a wall clock: `tick` is called once per
    second of signal time, and every duration is a number of ticks. A slow
    process therefore runs the intersection slowly; it can never shorten a
    yellow, which is the failure that kills people.
    """

    def __init__(
        self,
        cycle: PhaseCycle,
        sensor: Sensor | None = None,
        timing: TimingStrategy | None = None,
        clock: Clock | None = None,
        yellow_ticks: int = 3,
        all_red_ticks: int = 2,
        walk_ticks: int = 8,
    ) -> None:
        if yellow_ticks < 1 or all_red_ticks < 1:
            raise ValueError("yellow and all-red clearance must be at least one tick each")
        shortest_max = min(phase.max_green for phase in cycle.phases)
        if walk_ticks > shortest_max:
            raise ValueError(f"walk interval {walk_ticks} exceeds the shortest max green")
        self._cycle = cycle
        self._sensor = sensor
        self._timing = timing or FixedTiming()
        self._clock = clock or SystemClock()
        self._yellow = yellow_ticks
        self._all_red = all_red_ticks
        self._walk = walk_ticks
        self._lights = {d: TrafficLight(d) for d in cycle.directions()}
        self._listeners: list[PhaseListener] = list(self._lights.values())
        self._index = 0
        self._stage = ControllerState.ALL_RED  # start dark-safe, first green after a clearance
        self._elapsed = 0
        self._duration = all_red_ticks
        self._ticks = 0
        self._pending: list[SignalCommand] = []
        self._pedestrian: set[str] = set()
        self._override: Direction | None = None
        self._log: list[SignalEvent] = []
        self._lock = threading.Lock()

    # -- the invoker side of Command ---------------------------------------------------
    def submit(self, command: SignalCommand) -> None:
        """Queue a press. It is executed on the next tick, never mid-transition."""
        with self._lock:
            self._pending.append(command)
            self._record(f"queued: {command.label()}")

    def pedestrian_call(self, direction: Direction) -> None:
        self._cycle.serving(direction)  # fail fast: this intersection has no such approach
        self.submit(PedestrianCall(direction, self._clock.now()))

    def request_emergency(self, direction: Direction) -> None:
        self._cycle.serving(direction)
        self.submit(EmergencyOverride(direction, self._clock.now()))

    def clear_emergency(self) -> None:
        self.submit(ClearOverride(self._clock.now()))

    # -- the tick ----------------------------------------------------------------------
    def tick(self) -> int:
        with self._lock:
            self._ticks += 1
            if self._stage is not ControllerState.MAINTENANCE:
                self._drain_commands()
                self._elapsed += 1
                if self._stage is ControllerState.EMERGENCY:
                    if self._override is None:
                        self._begin(ControllerState.YELLOW, self._yellow)
                elif self._elapsed >= self._duration:
                    self._advance()
            update = self._update()
            listeners = list(self._listeners)
        for listener in listeners:  # outside the lock
            listener.on_phase_changed(update)
        return self._ticks

    def run(self, ticks: int) -> None:
        for _ in range(ticks):
            self.tick()

    # -- operator ----------------------------------------------------------------------
    def enter_maintenance(self) -> None:
        with self._lock:
            self._override = None
            self._pending.clear()
            self._begin(ControllerState.MAINTENANCE, 0)
            update, listeners = self._update(), list(self._listeners)
        for listener in listeners:
            listener.on_phase_changed(update)

    def leave_maintenance(self) -> None:
        with self._lock:
            if self._stage is not ControllerState.MAINTENANCE:
                raise SignalStateError("the intersection is not in maintenance")
            self._begin(ControllerState.ALL_RED, self._all_red)  # resume through a clearance
            update, listeners = self._update(), list(self._listeners)
        for listener in listeners:
            listener.on_phase_changed(update)

    # -- reads --------------------------------------------------------------------------
    def subscribe(self, listener: PhaseListener) -> None:
        with self._lock:
            self._listeners.append(listener)
            update = self._update()
        listener.on_phase_changed(update)

    def light(self, direction: Direction) -> TrafficLight:
        return self._lights[direction]

    def stage(self) -> ControllerState:
        with self._lock:
            return self._stage

    def current_phase(self) -> str:
        with self._lock:
            return self._cycle.phase(self._index).name

    def green_directions(self) -> frozenset[Direction]:
        """The safety invariant is asserted against this on every tick of every test."""
        with self._lock:
            if self._stage in (ControllerState.GREEN, ControllerState.EMERGENCY):
                return frozenset(self._cycle.phase(self._index).movements)
            return frozenset()

    def pending_pedestrians(self) -> set[str]:
        with self._lock:
            return set(self._pedestrian)

    def event_log(self) -> list[str]:
        with self._lock:
            return [event.render() for event in self._log]

    # -- the command sink: called with `_lock` already held --------------------------------
    def apply_pedestrian_call(self, direction: Direction) -> str:
        phase = self._cycle.serving(direction)
        self._pedestrian.add(phase.name)
        self._record(f"pedestrian waiting on {direction}, phase {phase.name} cannot be skipped")
        return phase.name

    def apply_emergency(self, direction: Direction) -> str:
        self._cycle.serving(direction)  # UnknownApproachError before anything changes
        self._override = direction
        if self._stage is ControllerState.GREEN:
            if self._cycle.phase(self._index).serves(direction):
                self._begin(ControllerState.EMERGENCY, 0)  # already green: just hold it
            else:
                self._duration = self._elapsed  # end this green now, yellow on the next tick
                self._record(f"emergency for {direction}: cutting the green short")
        return direction.value

    def apply_clear_override(self) -> str:
        self._override = None
        self._record("override cleared, the ring resumes")
        return ""

    # -- the state machine: called with `_lock` already held --------------------------------
    def _drain_commands(self) -> None:
        """Apply the queue. A bad command is logged and dropped: `tick` must never raise."""
        pending, self._pending = self._pending, []
        for command in pending:
            try:
                command.apply(self)
            except HandbookError as exc:
                self._record(f"dropped {command.label()}: {exc}")

    def _advance(self) -> None:
        if self._stage in (ControllerState.GREEN, ControllerState.EMERGENCY):
            self._begin(ControllerState.YELLOW, self._yellow)
        elif self._stage is ControllerState.YELLOW:
            self._begin(ControllerState.ALL_RED, self._all_red)
        else:
            self._start_green()

    def _start_green(self) -> None:
        if self._override is not None:
            phase = self._cycle.serving(self._override)
            self._index = self._cycle.index_of(phase.name)
            self._pedestrian.discard(phase.name)
            self._begin(ControllerState.EMERGENCY, 0)
            return
        self._index = self._next_index()
        phase = self._cycle.phase(self._index)
        demand = PhaseDemand(phase, self._waiting_for(phase), phase.name in self._pedestrian)
        green = phase.clamp(self._timing.green_ticks(demand))
        if demand.pedestrian_waiting:
            green = max(green, self._walk)
            self._pedestrian.discard(phase.name)
        self._begin(ControllerState.GREEN, green)

    def _next_index(self) -> int:
        """Skip phases nobody is waiting for; never skip one with a pedestrian call."""
        count = len(self._cycle)
        for step in range(1, count + 1):
            candidate = (self._index + step) % count
            phase = self._cycle.phase(candidate)
            if self._waiting_for(phase) or phase.name in self._pedestrian:
                return candidate
        return (self._index + 1) % count

    def _waiting_for(self, phase: Phase) -> int:
        if self._sensor is None:
            return 0
        return sum(self._sensor.waiting(direction) for direction in phase.movements)

    def _begin(self, stage: ControllerState, duration: int) -> None:
        self._stage = stage
        self._elapsed = 0
        self._duration = duration
        phase = self._cycle.phase(self._index).name
        detail = f" for {duration} ticks" if duration else ""
        self._record(f"{stage} on {phase}{detail}")

    def _update(self) -> SignalUpdate:
        phase = self._cycle.phase(self._index)
        return SignalUpdate(self._ticks, phase.name, frozenset(phase.movements), self._stage)

    def _record(self, message: str) -> None:
        self._log.append(SignalEvent(self._ticks, self._clock.now(), message))


# --8<-- [end:controller]
