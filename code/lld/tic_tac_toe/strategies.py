"""How a move gets chosen: the Strategy seam between a human, a dice-roll bot and a perfect bot."""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable
from typing import Protocol

from common import InvalidStateError, ValidationError
from lld.tic_tac_toe.models import Board, Cell, Symbol

WIN_SCORE = 100


def opponent(symbol: Symbol) -> Symbol:
    return Symbol.NOUGHT if symbol is Symbol.CROSS else Symbol.CROSS


# --8<-- [start:strategy]
class MoveStrategy(Protocol):
    """One method, one decision. The game never asks *what kind* of player this is."""

    def choose(self, board: Board, symbol: Symbol) -> Cell: ...


class ScriptedMove:
    """A queue of moves: a replayed game in tests, a UI's input buffer in production."""

    def __init__(self, cells: Iterable[Cell] = ()) -> None:
        self._queue: deque[Cell] = deque(cells)

    def offer(self, cell: Cell) -> None:
        self._queue.append(cell)

    def choose(self, board: Board, symbol: Symbol) -> Cell:
        if not self._queue:
            raise InvalidStateError(f"no scripted move left for {symbol}")
        return self._queue.popleft()


class RandomMove:
    """A legal move, uniformly at random. The generator is injected, so tests are exact."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def choose(self, board: Board, symbol: Symbol) -> Cell:
        free = board.free_cells()
        if not free:
            raise InvalidStateError("board is full")
        return self._rng.choice(free)


# --8<-- [end:strategy]


# --8<-- [start:minimax]
class MinimaxMove:
    """Perfect play on a 3x3 board: negamax with a transposition table.

    Search is make/unmake on the live board rather than a copy per node - the game
    lock is held while the bot thinks, so no other thread can observe the transient
    position. Scores decay by one per ply on the way up, so the bot prefers a win in
    three moves over the same win in five, and a memo entry stays valid whatever
    depth it was first computed at. Anything larger than 3x3 delegates to
    ``fallback``, because the game tree stops being enumerable.
    """

    def __init__(self, max_size: int = 3, fallback: MoveStrategy | None = None) -> None:
        self._max_size = max_size
        self._fallback = fallback or RandomMove(random.Random(0))
        self._memo: dict[tuple[tuple[Symbol | None, ...], Symbol], int] = {}

    def choose(self, board: Board, symbol: Symbol) -> Cell:
        if board.size > self._max_size:
            return self._fallback.choose(board, symbol)
        free = board.free_cells()
        if not free:
            raise InvalidStateError("board is full")
        best_cell, best_score = free[0], -WIN_SCORE - 1
        for cell in free:
            if board.place(cell, symbol):  # winning now beats every alternative
                board.clear(cell)
                return cell
            score = -self._negamax(board, opponent(symbol))
            board.clear(cell)
            if score > best_score:  # strict > keeps the choice deterministic
                best_cell, best_score = cell, score
        return best_cell

    def _negamax(self, board: Board, to_move: Symbol) -> int:
        """Score the position for ``to_move``: positive means to_move wins, sooner is larger."""
        key = (board.snapshot(), to_move)
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        free = board.free_cells()
        if not free:
            self._memo[key] = 0
            return 0
        best = -WIN_SCORE
        for cell in free:
            if board.place(cell, to_move):
                board.clear(cell)
                best = WIN_SCORE
                break
            score = -self._negamax(board, opponent(to_move))
            board.clear(cell)
            best = max(best, score)
        decayed = best - 1 if best > 0 else best + 1 if best < 0 else 0
        self._memo[key] = decayed
        return decayed


# --8<-- [end:minimax]


def require_rng(rng: random.Random | None) -> random.Random:
    """Bots must be seeded from outside; an implicit global generator is untestable."""
    if rng is None:
        raise ValidationError("a random bot needs an injected random.Random(seed)")
    return rng
