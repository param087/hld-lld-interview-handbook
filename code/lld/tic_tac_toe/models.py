"""Board, cells, symbols, moves and the O(1) win checker.

The rules live in ``services.py``; this module is the vocabulary the interviewer
expects on the whiteboard before any behaviour is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from common import ConflictError, InvalidStateError, ValidationError

if TYPE_CHECKING:  # imported for typing only; avoids a models <-> strategies cycle
    from lld.tic_tac_toe.strategies import MoveStrategy

DEFAULT_SIZE = 3


# --8<-- [start:enums]
class Symbol(StrEnum):
    CROSS = "X"
    NOUGHT = "O"


class PlayerKind(StrEnum):
    HUMAN = "human"  # moves arrive from outside: a UI, a socket, a test
    RANDOM_BOT = "random_bot"
    PERFECT_BOT = "perfect_bot"  # minimax, 3x3 only


class OffBoardError(ValidationError):
    """The cell is outside the N x N grid."""


class CellOccupiedError(ConflictError):
    """The cell already holds a symbol."""


class NothingToUndoError(InvalidStateError):
    """Undo was called on a game with no moves left to take back."""


# --8<-- [end:enums]


# --8<-- [start:values]
@dataclass(frozen=True, slots=True, order=True)
class Cell:
    """A coordinate, not a container: the board stores what is in it."""

    row: int
    col: int

    def __str__(self) -> str:
        return f"({self.row},{self.col})"


@dataclass(frozen=True, slots=True)
class Move:
    """The Command record: enough to replay the game and to invert one step."""

    number: int  # 1-based turn number
    player_id: str
    symbol: Symbol
    cell: Cell


@dataclass(frozen=True, slots=True)
class Player:
    """Who plays, with which symbol, and how the move is chosen (Strategy)."""

    id: str
    symbol: Symbol
    kind: PlayerKind
    strategy: MoveStrategy

    def next_move(self, board: Board) -> Cell:
        return self.strategy.choose(board, self.symbol)


# --8<-- [end:values]


# --8<-- [start:win_checker]
class WinChecker:
    """Incremental row, column and diagonal counters: O(1) per move, no rescan.

    A naive checker rescans 2N + 2 lines of N cells after every move - O(N^2) a
    move, O(N^4) over a full board. Here each placement bumps at most four
    counters and compares them with N. The centre of an odd board sits on *both* diagonals, so the two
    diagonal tests are separate ``if``s, never an ``elif`` - that is the bug
    interviewers look for.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self._rows: dict[Symbol, list[int]] = {s: [0] * size for s in Symbol}
        self._cols: dict[Symbol, list[int]] = {s: [0] * size for s in Symbol}
        self._diagonal: dict[Symbol, int] = dict.fromkeys(Symbol, 0)
        self._anti_diagonal: dict[Symbol, int] = dict.fromkeys(Symbol, 0)

    def record(self, cell: Cell, symbol: Symbol) -> bool:
        """Count the placement and report whether it completed a line."""
        self._rows[symbol][cell.row] += 1
        self._cols[symbol][cell.col] += 1
        if cell.row == cell.col:
            self._diagonal[symbol] += 1
        if cell.row + cell.col == self.size - 1:
            self._anti_diagonal[symbol] += 1
        return (
            self._rows[symbol][cell.row] == self.size
            or self._cols[symbol][cell.col] == self.size
            or self._diagonal[symbol] == self.size
            or self._anti_diagonal[symbol] == self.size
        )

    def erase(self, cell: Cell, symbol: Symbol) -> None:
        """The exact inverse of ``record``; this is what makes undo cheap."""
        self._rows[symbol][cell.row] -= 1
        self._cols[symbol][cell.col] -= 1
        if cell.row == cell.col:
            self._diagonal[symbol] -= 1
        if cell.row + cell.col == self.size - 1:
            self._anti_diagonal[symbol] -= 1

    def reset(self) -> None:
        for symbol in Symbol:
            self._rows[symbol] = [0] * self.size
            self._cols[symbol] = [0] * self.size
            self._diagonal[symbol] = 0
            self._anti_diagonal[symbol] = 0


# --8<-- [end:win_checker]


# --8<-- [start:board]
class Board:
    """The N x N grid plus its win checker. Knows placement rules, not game rules."""

    def __init__(self, size: int = DEFAULT_SIZE) -> None:
        if size < 3:
            raise ValidationError(f"board size must be at least 3, got {size}")
        self.size = size
        self._grid: list[list[Symbol | None]] = [[None] * size for _ in range(size)]
        self._checker = WinChecker(size)
        self._filled = 0

    def at(self, cell: Cell) -> Symbol | None:
        self._require_inside(cell)
        return self._grid[cell.row][cell.col]

    def place(self, cell: Cell, symbol: Symbol) -> bool:
        """Place a symbol; return True when the move completes a line."""
        self._require_inside(cell)
        if self._grid[cell.row][cell.col] is not None:
            raise CellOccupiedError(f"cell {cell} already holds {self._grid[cell.row][cell.col]}")
        self._grid[cell.row][cell.col] = symbol
        self._filled += 1
        return self._checker.record(cell, symbol)

    def clear(self, cell: Cell) -> Symbol:
        """Take a symbol back off the board (undo). Returns the symbol removed."""
        self._require_inside(cell)
        symbol = self._grid[cell.row][cell.col]
        if symbol is None:
            raise InvalidStateError(f"cell {cell} is empty")
        self._grid[cell.row][cell.col] = None
        self._filled -= 1
        self._checker.erase(cell, symbol)
        return symbol

    def free_cells(self) -> list[Cell]:
        return [
            Cell(r, c) for r in range(self.size) for c in range(self.size) if self._grid[r][c] is None
        ]

    def is_full(self) -> bool:
        return self._filled == self.size * self.size

    def reset(self) -> None:
        self._grid = [[None] * self.size for _ in range(self.size)]
        self._checker.reset()
        self._filled = 0

    def snapshot(self) -> tuple[Symbol | None, ...]:
        """Flat, hashable copy - the key a minimax transposition table needs."""
        return tuple(cell for row in self._grid for cell in row)

    def render(self) -> str:
        """A dot for an empty cell, so a rendered board never ends in trailing spaces."""
        return "\n".join("|".join(c.value if c else "." for c in row) for row in self._grid)

    def _require_inside(self, cell: Cell) -> None:
        if not (0 <= cell.row < self.size and 0 <= cell.col < self.size):
            raise OffBoardError(f"cell {cell} is outside a {self.size}x{self.size} board")


# --8<-- [end:board]
