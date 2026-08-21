"""One short game: eat, chase your own tail legally, refuse a reversal, pause, hit a wall."""

from common import FakeClock
from lld.snake_game.models import Direction, GameState, Grid, Point, Snake, TickResult
from lld.snake_game.services import Pause, Resume, SnakeGame, TextRenderer, Turn
from lld.snake_game.strategies import AcceleratingSpeed, ScriptedFoodSpawner


def build() -> tuple[SnakeGame, TextRenderer, FakeClock]:
    grid = Grid(10, 5, frozenset({Point(8, 1), Point(8, 2)}))
    snake = Snake([Point(3, 2), Point(2, 2), Point(1, 2)])
    spawner = ScriptedFoodSpawner([Point(5, 2), Point(0, 0)])
    clock = FakeClock(start=1_000.0)
    game = SnakeGame(grid, snake, spawner, Direction.RIGHT, speed=AcceleratingSpeed(), clock=clock)
    renderer = TextRenderer()
    game.subscribe(renderer)
    return game, renderer, clock


def main() -> None:
    game, renderer, clock = build()
    print("--- 10 x 5 grid, scripted food, a snake of 3 heading right ---")
    print(game.render())

    def step() -> TickResult:
        result = game.tick()
        clock.advance(game.next_interval())
        return result

    before = game.next_interval()
    step()
    eaten = step()
    print(f"tick {eaten.tick}: ate at (5,2), score {eaten.score}, length {len(game.snake)}, "
          f"interval {before:.2f}s to {game.next_interval():.2f}s")

    for direction in (Direction.UP, Direction.LEFT, Direction.DOWN):
        game.submit(Turn(direction))
        step()
    print(f"tick {game.tick_count}: moved into the cell the tail was vacating, which is legal")
    print(game.render())

    print(f"UP submitted after DOWN: accepted={game.submit(Turn(Direction.UP))} (a 180-degree reversal is ignored)")
    game.submit(Pause())
    paused = game.tick()
    print(f"paused: tick still {paused.tick}, moved={paused.moved}, state={paused.state}")
    game.submit(Resume())

    last = TickResult(0, game.state, moved=False, ate=False, score=0)
    while game.state is not GameState.OVER:
        last = step()
    print(f"tick {last.tick}: over by {last.reason}, score {last.score}, length {len(game.snake)}, "
          f"{len(renderer.frames())} frames in {game.elapsed():.2f}s of game time")


if __name__ == "__main__":
    main()
