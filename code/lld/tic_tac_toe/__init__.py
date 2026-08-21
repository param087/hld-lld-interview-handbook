"""Tic-tac-toe, and the ``BoardGame`` template that snake and ladder and bowling reuse."""

from lld.tic_tac_toe.base import (
    TERMINAL_STATUSES,
    BoardGame,
    GameEvent,
    GameLog,
    GameObserver,
    GameResult,
    GameStatus,
    NotYourTurnError,
    TurnCursor,
    TurnLimitError,
)
from lld.tic_tac_toe.models import (
    Board,
    Cell,
    CellOccupiedError,
    Move,
    NothingToUndoError,
    OffBoardError,
    Player,
    PlayerKind,
    Symbol,
    WinChecker,
)
from lld.tic_tac_toe.services import BoardRenderer, PlayerFactory, TicTacToeGame
from lld.tic_tac_toe.strategies import (
    MinimaxMove,
    MoveStrategy,
    RandomMove,
    ScriptedMove,
)

__all__ = [
    "TERMINAL_STATUSES",
    "Board",
    "BoardGame",
    "BoardRenderer",
    "Cell",
    "CellOccupiedError",
    "GameEvent",
    "GameLog",
    "GameObserver",
    "GameResult",
    "GameStatus",
    "MinimaxMove",
    "Move",
    "MoveStrategy",
    "NotYourTurnError",
    "NothingToUndoError",
    "OffBoardError",
    "Player",
    "PlayerFactory",
    "PlayerKind",
    "RandomMove",
    "ScriptedMove",
    "Symbol",
    "TicTacToeGame",
    "TurnCursor",
    "TurnLimitError",
    "WinChecker",
]
