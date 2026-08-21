import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import InvalidStateError, ValidationError
from lld.snake_game.models import (
    Direction,
    EndReason,
    Food,
    GameOverError,
    GameState,
    Grid,
    Point,
    Snake,
)
from lld.snake_game.services import Pause, Resume, SnakeGame, TextRenderer, Turn
from lld.snake_game.strategies import (
    AcceleratingSpeed,
    ConstantSpeed,
    RandomFoodSpawner,
    ScriptedFoodSpawner,
)


def simple_game(direction: Direction = Direction.RIGHT) -> SnakeGame:
    """A 3-cell snake in the middle of an 8 x 5 grid, with food two cells to its right."""
    return SnakeGame(
        Grid(8, 5),
        Snake([Point(2, 2), Point(1, 2), Point(0, 2)]),
        ScriptedFoodSpawner([Point(4, 2), Point(6, 0)]),
        direction,
    )


def test_a_scripted_game_eats_grows_and_publishes_frames() -> None:
    game = simple_game()
    renderer = TextRenderer()
    game.subscribe(renderer)

    first, second = game.tick(), game.tick()

    assert (first.ate, second.ate) == (False, True)
    assert second.score == 1 and len(game.snake) == 4
    assert game.food == Food(Point(6, 0))  # the spawner moved on to its next position
    frames = renderer.frames()
    assert len(frames) == 2 and frames[-1].board.splitlines()[2] == ".oooO..."


# --8<-- [start:tail]
def test_the_tail_cell_is_free_only_while_the_snake_is_not_growing() -> None:
    snake = Snake([Point(2, 2), Point(2, 1), Point(3, 1), Point(3, 2)])

    assert snake.would_collide(snake.tail, grow=False) is False  # the tail leaves this tick
    assert snake.would_collide(snake.tail, grow=True) is True  # growing keeps the tail put
    assert snake.would_collide(Point(2, 1), grow=False) is True  # a mid-body cell is always fatal


def test_moving_into_the_cell_the_tail_is_vacating_is_legal() -> None:
    game = SnakeGame(
        Grid(8, 5),
        Snake([Point(2, 2), Point(2, 1), Point(3, 1), Point(3, 2)]),
        ScriptedFoodSpawner([Point(7, 4)]),
        Direction.DOWN,
    )
    game.submit(Turn(Direction.RIGHT))  # head (2,2) turns onto the tail cell (3,2)

    result = game.tick()

    assert result.moved and game.state is GameState.RUNNING
    assert game.snake.head == Point(3, 2) and len(game.snake) == 4


# --8<-- [end:tail]


# --8<-- [start:input]
def test_reversals_repeats_and_a_full_buffer_are_all_refused() -> None:
    game = simple_game(Direction.RIGHT)

    assert game.submit(Turn(Direction.LEFT)) is False  # reverses the applied direction
    assert game.submit(Turn(Direction.RIGHT)) is False  # no-op
    assert game.submit(Turn(Direction.UP)) is True
    assert game.submit(Turn(Direction.DOWN)) is False  # reverses the *queued* UP, not RIGHT
    assert game.submit(Turn(Direction.LEFT)) is True
    assert game.submit(Turn(Direction.UP)) is False  # buffer holds at most two turns

    assert game.tick().moved and game.direction is Direction.UP
    assert game.tick().moved and game.direction is Direction.LEFT


# --8<-- [end:input]


@pytest.mark.parametrize(
    ("grid", "body", "direction", "reason"),
    [
        (Grid(5, 5), [Point(0, 2)], Direction.LEFT, EndReason.WALL),
        (Grid(5, 5, frozenset({Point(1, 2)})), [Point(0, 2)], Direction.RIGHT, EndReason.OBSTACLE),
        (
            Grid(5, 5),
            [Point(2, 2), Point(3, 2), Point(3, 3), Point(2, 3), Point(1, 3)],
            Direction.DOWN,
            EndReason.SELF,
        ),
    ],
)
def test_every_collision_ends_the_game_with_its_reason(
    grid: Grid, body: list[Point], direction: Direction, reason: EndReason
) -> None:
    game = SnakeGame(grid, Snake(body), ScriptedFoodSpawner([Point(4, 4)]), direction)
    result = game.tick()
    assert (result.reason, result.moved, game.state) == (reason, False, GameState.OVER)


def test_filling_the_grid_ends_the_game_as_a_win() -> None:
    body = [
        Point(1, 2), Point(0, 2), Point(0, 1), Point(1, 1),
        Point(2, 1), Point(2, 0), Point(1, 0), Point(0, 0),
    ]  # eight of the nine cells; only (2,2) is free
    game = SnakeGame(Grid(3, 3), Snake(body), RandomFoodSpawner(random.Random(1)), Direction.RIGHT)
    assert game.food == Food(Point(2, 2))

    result = game.tick()

    assert (result.ate, result.reason, result.score) == (True, EndReason.FILLED, 1)
    assert len(game.snake) == 9 and game.state is GameState.OVER


def test_pause_resume_and_finished_game_transitions() -> None:
    game = simple_game()
    with pytest.raises(InvalidStateError):
        game.pause()  # a game that has not started cannot be paused

    game.tick()
    assert game.state is GameState.RUNNING
    assert game.submit(Pause()) is True and game.state is GameState.PAUSED
    idle = game.tick()
    assert (idle.moved, idle.tick) == (False, 1)  # a paused tick costs nothing
    assert game.submit(Resume()) is True

    while game.state is not GameState.OVER:
        game.tick()
    with pytest.raises(GameOverError):
        game.tick()
    with pytest.raises(GameOverError):
        game.submit(Turn(Direction.UP))
    with pytest.raises(InvalidStateError):
        game.pause()


# --8<-- [start:concurrency]
def test_input_from_other_threads_never_lands_mid_tick() -> None:
    game = SnakeGame(
        Grid(20, 20),
        Snake([Point(10, 10), Point(9, 10), Point(8, 10)]),
        RandomFoodSpawner(random.Random(5)),
        Direction.RIGHT,
    )
    applied: list[Direction] = []
    keys = list(Direction)

    def ticker() -> None:
        for _ in range(80):
            try:
                game.tick()
            except GameOverError:
                return
            applied.append(game.direction)  # only this thread appends

    def presser(i: int) -> None:
        for _ in range(80):
            try:
                game.submit(Turn(keys[i % 4]))
            except GameOverError:
                return

    with ThreadPoolExecutor(max_workers=5) as pool:
        for future in [pool.submit(ticker), *(pool.submit(presser, i) for i in range(4))]:
            future.result()

    cells = game.snake.cells()
    assert len(set(cells)) == len(cells)  # the deque and the membership set never diverged
    assert len(applied) > 1
    assert all(a is not b.opposite for a, b in zip(applied, applied[1:], strict=False))


# --8<-- [end:concurrency]


def test_grid_snake_and_placement_reject_impossible_setups() -> None:
    with pytest.raises(ValidationError):
        Grid(2, 5)
    with pytest.raises(ValidationError):
        Grid(5, 5, frozenset({Point(9, 9)}))
    with pytest.raises(ValidationError):
        Snake([])
    with pytest.raises(ValidationError):
        Snake([Point(1, 1), Point(1, 1)])
    with pytest.raises(ValidationError):
        SnakeGame(Grid(5, 5, frozenset({Point(1, 1)})), Snake([Point(1, 1)]), ScriptedFoodSpawner([]))


def test_speed_scales_with_score_and_spawning_is_reproducible() -> None:
    speed = AcceleratingSpeed(base=0.2, factor=0.9, floor=0.06)
    assert speed.interval(0, 3) == pytest.approx(0.2)
    assert speed.interval(11, 14) == pytest.approx(0.2 * 0.9**11)
    assert speed.interval(12, 15) == pytest.approx(0.06)  # 0.2 x 0.9^12 = 0.056, clamped
    assert ConstantSpeed(0.1).interval(99, 99) == pytest.approx(0.1)
    with pytest.raises(ValidationError):
        AcceleratingSpeed(base=0.1, factor=1.5)

    grid, occupied = Grid(6, 6), [Point(0, 0)]
    one, two = RandomFoodSpawner(random.Random(9)), RandomFoodSpawner(random.Random(9))
    assert [one.spawn(grid, occupied) for _ in range(5)] == [two.spawn(grid, occupied) for _ in range(5)]
