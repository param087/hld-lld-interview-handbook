"""The engine: four State classes, one tick, an input buffer and a renderer port."""

from __future__ import annotations

import threading
from abc import ABC
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from common import Clock, InvalidStateError, SystemClock, ValidationError
from lld.snake_game.models import (
    Direction,
    EndReason,
    Food,
    Frame,
    GameOverError,
    GameState,
    Grid,
    Point,
    Snake,
    TickResult,
)
from lld.snake_game.strategies import ConstantSpeed, FoodSpawner, SpeedPolicy


# --8<-- [start:renderer]
class Renderer(Protocol):
    """The output port. A curses front end and a websocket both implement this one method."""

    def on_frame(self, frame: Frame) -> None: ...


class TextRenderer:
    """Observer: keeps every frame the engine published. Never reaches back into the game."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames: list[Frame] = []

    def on_frame(self, frame: Frame) -> None:
        with self._lock:
            self._frames.append(frame)

    def frames(self) -> tuple[Frame, ...]:
        with self._lock:
            return tuple(self._frames)

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frames[-1] if self._frames else None


# --8<-- [end:renderer]


# --8<-- [start:phases]
class GamePhase(ABC):
    """State pattern: the phase decides what tick, pause and resume mean.

    Here it earns its place, unlike the enum-plus-guards in tic-tac-toe: a paused
    tick is a no-op, a running tick advances the world, and a finished tick is an
    error. Three different behaviours behind one call, with no ``if state ==``
    ladder anywhere in the engine.
    """

    state: GameState

    def tick(self, game: SnakeGame) -> TickResult:
        raise InvalidStateError(f"cannot tick a game that is {self.state}")

    def pause(self, game: SnakeGame) -> GamePhase:
        raise InvalidStateError(f"cannot pause a game that is {self.state}")

    def resume(self, game: SnakeGame) -> GamePhase:
        raise InvalidStateError(f"cannot resume a game that is {self.state}")


class ReadyPhase(GamePhase):
    state = GameState.READY

    def tick(self, game: SnakeGame) -> TickResult:
        game.transition(RunningPhase())
        return game.phase.tick(game)


class RunningPhase(GamePhase):
    state = GameState.RUNNING

    def tick(self, game: SnakeGame) -> TickResult:
        return game.advance()

    def pause(self, game: SnakeGame) -> GamePhase:
        return PausedPhase()

    def resume(self, game: SnakeGame) -> GamePhase:
        return self


class PausedPhase(GamePhase):
    state = GameState.PAUSED

    def tick(self, game: SnakeGame) -> TickResult:
        return game.idle()  # a tick while paused moves nothing and costs nothing

    def pause(self, game: SnakeGame) -> GamePhase:
        return self

    def resume(self, game: SnakeGame) -> GamePhase:
        return RunningPhase()


class OverPhase(GamePhase):
    state = GameState.OVER

    def __init__(self, reason: EndReason) -> None:
        self.reason = reason

    def tick(self, game: SnakeGame) -> TickResult:
        raise GameOverError(f"the game ended: {self.reason}")


# --8<-- [end:phases]


# --8<-- [start:engine]
class SnakeGame:
    """One tick moves the world forward by one cell. Everything else serves that method.

    ``_lock`` guards the snake, the food, the score, the phase and the input buffer.
    It exists because input and the clock are different threads in every real front
    end: a key press must not land halfway through a tick.
    """

    MAX_BUFFERED_INPUTS = 2

    def __init__(
        self,
        grid: Grid,
        snake: Snake,
        spawner: FoodSpawner,
        direction: Direction = Direction.RIGHT,
        *,
        speed: SpeedPolicy | None = None,
        clock: Clock | None = None,
    ) -> None:
        for cell in snake.cells():
            if not grid.contains(cell) or grid.is_obstacle(cell):
                raise ValidationError(f"the snake starts on {cell}, which is not a free cell")
        self.grid = grid
        self.snake = snake
        self._spawner = spawner
        self._speed = speed or ConstantSpeed()
        self._clock = clock or SystemClock()
        self._direction = direction
        self._inputs: deque[Direction] = deque()
        self._phase: GamePhase = ReadyPhase()
        self._food: Food | None = spawner.spawn(grid, snake.cells())
        self._score = 0
        self._tick = 0
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._lock = threading.RLock()
        self._observers: list[Renderer] = []
        self._buffer: list[Frame] = []

    # -- state, read-only to callers ----------------------------------------------------
    @property
    def phase(self) -> GamePhase:
        return self._phase

    @property
    def state(self) -> GameState:
        with self._lock:
            return self._phase.state

    @property
    def tick_count(self) -> int:
        with self._lock:
            return self._tick

    @property
    def score(self) -> int:
        with self._lock:
            return self._score

    @property
    def food(self) -> Food | None:
        with self._lock:
            return self._food

    @property
    def direction(self) -> Direction:
        with self._lock:
            return self._direction

    def next_interval(self) -> float:
        with self._lock:
            return self._speed.interval(self._score, len(self.snake))

    def elapsed(self) -> float:
        with self._lock:
            if self._started_at is None:
                return 0.0
            return (self._ended_at or self._clock.now()) - self._started_at

    # -- the public API ------------------------------------------------------------------
    def tick(self) -> TickResult:
        try:
            with self._lock:
                return self._phase.tick(self)
        finally:
            self.flush()

    def submit(self, command: InputCommand) -> bool:
        """Commands are applied between ticks, never inside one - that is what the lock buys."""
        return command.apply(self)

    def change_direction(self, direction: Direction) -> bool:
        """Buffer a turn. Rejected if it repeats or reverses the last *queued* direction.

        Validating against the queue rather than the applied direction is the fix for
        the classic bug: pressing UP then LEFT inside one tick must not let LEFT be
        checked against RIGHT, which was two turns ago.
        """
        with self._lock:
            if self._phase.state is GameState.OVER:
                raise GameOverError("the game has ended")
            previous = self._inputs[-1] if self._inputs else self._direction
            if direction is previous or direction.reverses(previous):
                return False
            if len(self._inputs) >= self.MAX_BUFFERED_INPUTS:
                return False
            self._inputs.append(direction)
            return True

    def pause(self) -> GameState:
        try:
            with self._lock:
                self.transition(self._phase.pause(self))
                self._publish()
                return self._phase.state
        finally:
            self.flush()

    def resume(self) -> GameState:
        try:
            with self._lock:
                self.transition(self._phase.resume(self))
                self._publish()
                return self._phase.state
        finally:
            self.flush()

    def transition(self, phase: GamePhase) -> None:
        self._phase = phase

    # -- what a tick actually does --------------------------------------------------------
    def advance(self) -> TickResult:
        """Wall, then obstacle, then self; move; then eat and respawn."""
        self._tick += 1
        if self._started_at is None:
            self._started_at = self._clock.now()
        direction = self._inputs.popleft() if self._inputs else self._direction
        self._direction = direction
        target = self.snake.next_head(direction)
        if not self.grid.contains(target):
            return self._end(EndReason.WALL)
        if self.grid.is_obstacle(target):
            return self._end(EndReason.OBSTACLE)
        food = self._food
        grow = food is not None and target == food.position
        if self.snake.would_collide(target, grow):
            return self._end(EndReason.SELF)
        self.snake.move(target, grow)
        if food is None or not grow:
            return self._result(moved=True, ate=False)
        self._score += food.value
        self._food = self._spawner.spawn(self.grid, self.snake.cells())
        if self._food is None:  # the snake filled the grid: there is nowhere left to eat
            return self._end(EndReason.FILLED, moved=True, ate=True)
        return self._result(moved=True, ate=True)

    def idle(self) -> TickResult:
        return TickResult(self._tick, self._phase.state, moved=False, ate=False, score=self._score)

    def render(self) -> str:
        with self._lock:
            cells: dict[Point, str] = dict.fromkeys(self.snake.cells(), "o")
            cells[self.snake.head] = "O"
            for obstacle in self.grid.obstacles:
                cells[obstacle] = "#"
            if self._food is not None:
                cells[self._food.position] = "*"
            return "\n".join(
                "".join(cells.get(Point(x, y), ".") for x in range(self.grid.width))
                for y in range(self.grid.height)
            )

    # -- observers -------------------------------------------------------------------------
    def subscribe(self, renderer: Renderer) -> None:
        with self._lock:
            self._observers.append(renderer)

    def flush(self) -> None:
        """Publish with no lock held: a slow front end never delays the next tick."""
        with self._lock:
            frames, self._buffer = self._buffer, []
            observers = tuple(self._observers)
        for frame in frames:
            for observer in observers:
                observer.on_frame(frame)

    def _publish(self) -> None:
        self._buffer.append(Frame(self._tick, self._phase.state, self._score, self.render()))

    def _result(self, *, moved: bool, ate: bool, reason: EndReason | None = None) -> TickResult:
        self._publish()
        return TickResult(self._tick, self._phase.state, moved, ate, self._score, reason)

    def _end(self, reason: EndReason, *, moved: bool = False, ate: bool = False) -> TickResult:
        self._ended_at = self._clock.now()
        self.transition(OverPhase(reason))
        return self._result(moved=moved, ate=ate, reason=reason)


# --8<-- [end:engine]


# --8<-- [start:commands]
class InputCommand(Protocol):
    """One key press as an object: queueable, loggable, replayable, testable."""

    def apply(self, game: SnakeGame) -> bool: ...


@dataclass(frozen=True, slots=True)
class Turn:
    direction: Direction

    def apply(self, game: SnakeGame) -> bool:
        return game.change_direction(self.direction)


@dataclass(frozen=True, slots=True)
class Pause:
    def apply(self, game: SnakeGame) -> bool:
        return game.pause() is GameState.PAUSED


@dataclass(frozen=True, slots=True)
class Resume:
    def apply(self, game: SnakeGame) -> bool:
        return game.resume() is GameState.RUNNING


# --8<-- [end:commands]
