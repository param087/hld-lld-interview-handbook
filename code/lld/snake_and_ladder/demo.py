"""A seeded game on the classic board, the three overshoot rules, and two rejected boards."""

import random

from lld.snake_and_ladder.models import (
    Board,
    GameConfig,
    InvalidJumpError,
    Jump,
    OvershootRule,
    Position,
)
from lld.snake_and_ladder.services import CLASSIC_JUMPS, BoardFactory, SnakeAndLadderGame
from lld.snake_and_ladder.strategies import FairDice
from lld.tic_tac_toe.base import GameLog


def main() -> None:
    game = SnakeAndLadderGame(
        ["Ana", "Bo", "Cy"],
        FairDice(random.Random(42)),
        BoardFactory.classic(),
    )
    log = GameLog()
    game.subscribe(log)
    result = game.play()

    print("--- classic board, three players, one seeded six-sided die ---")
    for record in [r for r in game.records() if r.jumps][:6]:
        print(f"[{record.number:>2}] {record.player} rolls {record.roll}: {record.start.square} to {record.end.square} via {record.jumps[0]}")
    print(f"winner: {result.winner} after {result.turns} turns, ranking {game.ranking()}")
    print(f"the log observer recorded {len(log.events())} events")

    print("--- rolling a 5 from square 97, one rule at a time ---")
    for rule in OvershootRule:
        board = Board(CLASSIC_JUMPS, GameConfig(overshoot=rule))
        landed, jumps = board.step(Position(97), 5)
        trail = f" via {jumps[0]}" if jumps else ""
        print(f"{rule.value:>6}: 97 + 5 lands on {landed.square}{trail}")

    print("--- boards rejected at construction, never at turn 40 ---")
    broken = {
        "overlap": ([Jump(10, 30), Jump(10, 5)], GameConfig(size=60)),
        "cycle": (
            [Jump(10, 30), Jump(30, 50), Jump(50, 10)],
            GameConfig(size=60, allow_chained_jumps=True),
        ),
    }
    for label, (jumps, config) in broken.items():
        try:
            Board(jumps, config)
        except InvalidJumpError as exc:
            print(f"{label}: {exc}")


if __name__ == "__main__":
    main()
