"""Snake and ladder: the same ``BoardGame`` template as tic-tac-toe, with dice."""

from lld.snake_and_ladder.models import (
    Board,
    GameConfig,
    GameFinishedError,
    InvalidJumpError,
    Jump,
    JumpKind,
    OvershootRule,
    Position,
    TurnRecord,
)
from lld.snake_and_ladder.services import CLASSIC_JUMPS, BoardFactory, SnakeAndLadderGame
from lld.snake_and_ladder.strategies import DiceStrategy, FairDice, LoadedDice, ScriptedDice

__all__ = [
    "CLASSIC_JUMPS",
    "Board",
    "BoardFactory",
    "DiceStrategy",
    "FairDice",
    "GameConfig",
    "GameFinishedError",
    "InvalidJumpError",
    "Jump",
    "JumpKind",
    "LoadedDice",
    "OvershootRule",
    "Position",
    "ScriptedDice",
    "SnakeAndLadderGame",
    "TurnRecord",
]
