"""Prototype: make a new object by copying a configured one instead of building it.

The running example is a game-tree search over a miniature board. ``Board`` (the
Concrete Prototype) knows how to produce an independent copy of itself, so
``MoveSearch`` can fork a position per branch and throw the fork away.
``PrototypeRegistry`` holds named, already-configured boards and answers
``create`` by cloning one, so no builder or subclass exists per position. The
last section shows the Pythonic forms -- ``copy.deepcopy``,
``dataclasses.replace`` and the ``__deepcopy__`` hook that declares what must be
shared rather than copied -- which is where most Python code should start.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol, Self, runtime_checkable

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError

FILES = "abcd"
RANKS = "1234"
ORTHOGONAL_STEPS = ((0, 1), (0, -1), (1, 0), (-1, 0))
SEARCH_DEPTH = 3


# --8<-- [start:prototype]
@runtime_checkable
class Prototype(Protocol):
    """The interface: an object that can hand back an independent copy of itself.

    ``Self`` ties the return type to the receiver, so a clone of a ``Board`` is a
    ``Board``. A ``Protocol`` rather than an ``ABC``: nothing inherits from this,
    a class qualifies by having a matching ``clone`` method.
    """

    def clone(self) -> Self: ...


class Side(StrEnum):
    WHITE = "white"
    BLACK = "black"

    def other(self) -> Side:
        return Side.BLACK if self is Side.WHITE else Side.WHITE


class PieceKind(StrEnum):
    KING = "k"
    ROOK = "r"


type Square = str
type Move = tuple[Square, Square]


@dataclass(frozen=True, slots=True)
class Piece:
    """Immutable, therefore shared by every clone instead of copied into each one.

    This is the decision that makes cloning cheap: a board copies its *mapping*
    of squares to pieces, never the pieces, because no holder can change one.
    """

    kind: PieceKind
    side: Side

    @property
    def symbol(self) -> str:
        return self.kind.upper() if self.side is Side.WHITE else str(self.kind)


@dataclass(slots=True)
class Board:
    """The Concrete Prototype: mutable state that a search wants to fork cheaply.

    ``clone`` copies field by field and that is the whole pattern: a new ``dict``
    and a new ``list`` because both are mutable, the same ``Piece`` objects
    because they are frozen, and ``side_to_move`` by value. The cost is one
    sentence you can defend: O(occupied squares), no constructor logic re-run.
    """

    squares: dict[Square, Piece] = field(default_factory=dict)
    side_to_move: Side = Side.WHITE
    move_log: list[str] = field(default_factory=list)

    def clone(self) -> Board:
        """A new board that shares nothing mutable with this one."""
        return Board(dict(self.squares), self.side_to_move, list(self.move_log))

    def moves(self) -> list[Move]:
        """Every one-square orthogonal step for the side to move, in a fixed order."""
        found: list[Move] = []
        for source in sorted(self.squares):
            piece = self.squares[source]
            if piece.side is not self.side_to_move:
                continue
            for target in neighbours(source):
                occupant = self.squares.get(target)
                if occupant is None or occupant.side is not piece.side:
                    found.append((source, target))
        return found

    def apply(self, move: Move) -> Board:
        """Return a *new* board with the move made; the receiver is untouched."""
        child = self.clone()
        child.push(move)
        return child

    def push(self, move: Move) -> Piece | None:
        """Make the move in place and return the captured piece, if any.

        The return value is the undo record. Handing it to the caller keeps it on
        the recursion stack, which is why make/unmake needs no memory of its own.
        """
        source, target = move
        piece = self.squares.get(source)
        if piece is None:
            raise ValidationError(f"no piece on {source}")
        if piece.side is not self.side_to_move:
            raise InvalidStateError(f"{self.side_to_move} to move, not {piece.side}")
        captured = self.squares.pop(target, None)
        del self.squares[source]
        self.squares[target] = piece
        self.move_log.append(f"{piece.symbol}{source}{target}")
        self.side_to_move = self.side_to_move.other()
        return captured

    def pop(self, move: Move, captured: Piece | None) -> None:
        """Unmake the move: O(1), the alternative to a clone per branch."""
        source, target = move
        if target not in self.squares:
            raise InvalidStateError(f"nothing on {target} to unmake")
        self.squares[source] = self.squares.pop(target)
        if captured is not None:
            self.squares[target] = captured
        self.move_log.pop()
        self.side_to_move = self.side_to_move.other()

    def render(self) -> str:
        """One group of characters per rank, highest rank first."""
        ranks: list[str] = []
        for rank in reversed(RANKS):
            cells = []
            for file in FILES:
                piece = self.squares.get(f"{file}{rank}")
                cells.append(piece.symbol if piece is not None else ".")
            ranks.append("".join(cells))
        return " ".join(ranks)


def neighbours(square: Square) -> list[Square]:
    """The squares one orthogonal step away that are still on the board."""
    file_index, rank_index = FILES.index(square[0]), RANKS.index(square[1])
    targets = []
    for file_step, rank_step in ORTHOGONAL_STEPS:
        file, rank = file_index + file_step, rank_index + rank_step
        if 0 <= file < len(FILES) and 0 <= rank < len(RANKS):
            targets.append(f"{FILES[file]}{RANKS[rank]}")
    return sorted(targets)


# --8<-- [end:prototype]


# --8<-- [start:clients]
class PrototypeRegistry[P: Prototype]:
    """The Prototype Manager: named, already-configured instances that ``create`` clones.

    It never calls a constructor and never learns a class name, so adding a
    position is registering an object rather than writing a subclass or a
    builder. Handing out ``clone()`` and not the stored object is the invariant:
    a caller that mutated the prototype would poison every later ``create``.
    """

    def __init__(self) -> None:
        self._prototypes: dict[str, P] = {}

    def register(self, name: str, prototype: P) -> None:
        if name in self._prototypes:
            raise ConflictError(f"prototype {name!r} is already registered")
        self._prototypes[name] = prototype

    def create(self, name: str) -> P:
        prototype = self._prototypes.get(name)
        if prototype is None:
            raise NotFoundError(f"no prototype named {name!r}")
        return prototype.clone()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._prototypes))


class MoveSearch:
    """The Client: it explores a tree of positions and must not disturb the caller's board.

    ``count_by_cloning`` forks a board per branch, which is correct by
    construction and costs one clone per edge. ``count_in_place`` walks the same
    tree with make/unmake and allocates nothing. Both return the same number;
    which one you reach for is a cost decision, not a correctness one.
    """

    def __init__(self) -> None:
        self.clones = 0

    def count_by_cloning(self, board: Board, depth: int) -> int:
        if depth < 0:
            raise ValidationError("depth cannot be negative")
        if depth == 0:
            return 1
        total = 0
        for move in board.moves():
            self.clones += 1
            total += self.count_by_cloning(board.apply(move), depth - 1)
        return total

    def count_in_place(self, board: Board, depth: int) -> int:
        if depth < 0:
            raise ValidationError("depth cannot be negative")
        if depth == 0:
            return 1
        total = 0
        for move in board.moves():
            captured = board.push(move)
            total += self.count_in_place(board, depth - 1)
            board.pop(move, captured)
        return total


# --8<-- [end:clients]


# --8<-- [start:pythonic]
@dataclass(frozen=True, slots=True)
class GameSettings:
    """A frozen value object: ``dataclasses.replace`` is its clone-with-changes."""

    variant: str
    minutes: int
    increment_seconds: int
    rated: bool = True


def as_blitz(settings: GameSettings) -> GameSettings:
    """No ``clone`` method and no builder: one call returns a new value, two fields changed.

    ``replace`` re-runs ``__init__``, so ``__post_init__`` validation still fires
    and a misspelled field name is a ``TypeError`` at the call, not a silent
    attribute on the copy.
    """
    return replace(settings, minutes=3, increment_seconds=2)


class Engine:
    """An expensive shared resource. Copying one would be a bug, not a clone."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.evaluations = 0

    def evaluate(self, board: Board) -> int:
        self.evaluations += 1
        return sum(1 if piece.side is Side.WHITE else -1 for piece in board.squares.values())


@dataclass(slots=True)
class AnalysisSession:
    """``copy.deepcopy`` copies the entire reachable graph; ``__deepcopy__`` says what not to.

    Without the hook, deep-copying a session would duplicate the engine (and,
    in real code, the connection pool, the lock and the logger behind it).
    Writing ``memo[id(self)] = ...`` before returning is what keeps ``deepcopy``
    correct when the graph contains a cycle back to this object.
    """

    board: Board
    engine: Engine
    notes: list[str] = field(default_factory=list)

    def __deepcopy__(self, memo: dict[int, Any]) -> AnalysisSession:
        clone = AnalysisSession(self.board.clone(), self.engine, list(self.notes))
        memo[id(self)] = clone
        return clone


# --8<-- [end:pythonic]


def starting_position() -> Board:
    """The configured object the registry stores: two kings and two rooks on 4x4."""
    return Board(
        {
            "a1": Piece(PieceKind.ROOK, Side.WHITE),
            "b1": Piece(PieceKind.KING, Side.WHITE),
            "c4": Piece(PieceKind.KING, Side.BLACK),
            "d4": Piece(PieceKind.ROOK, Side.BLACK),
        }
    )


def main() -> None:
    registry: PrototypeRegistry[Board] = PrototypeRegistry()
    registry.register("duel", starting_position())
    registry.register("bare_kings", Board({"a1": Piece(PieceKind.KING, Side.WHITE)}))
    print("--- a registry of configured objects, not of constructors ---")
    print(f"registered: {registry.names}")
    first, second = registry.create("duel"), registry.create("duel")
    print(f"two creates, two objects: {first is not second}, equal state: {first == second}")
    first.push(("a1", "a2"))
    print(f"mutated the first:  {first.render()}")
    print(f"the second is cold: {second.render()}")
    print(f"the stored prototype is untouched: {registry.create('duel') == second}")

    print("--- clone copies the mutable containers and shares the frozen pieces ---")
    original = registry.create("duel")
    cloned = original.clone()
    print(f"same piece object: {cloned.squares['a1'] is original.squares['a1']}")
    print(f"different dict:    {cloned.squares is not original.squares}")
    shallow = copy.copy(original)
    shallow.push(("b1", "b2"))
    print(f"copy.copy shares the dict, so the original moved too: {original.render()}")
    print(f"and the halves disagree: {original.side_to_move} vs {shallow.side_to_move} to move")

    print("--- fork per branch, or make and unmake: same tree, different cost ---")
    board = registry.create("duel")
    forking, in_place = MoveSearch(), MoveSearch()
    by_cloning = forking.count_by_cloning(board, SEARCH_DEPTH)
    by_undo = in_place.count_in_place(board, SEARCH_DEPTH)
    print(f"depth {SEARCH_DEPTH}: {by_cloning} positions, {forking.clones} clones")
    print(f"depth {SEARCH_DEPTH}: {by_undo} positions, {in_place.clones} clones")
    print(f"the caller's board survived both: {board == registry.create('duel')}")

    print("--- the Pythonic forms: dataclasses.replace and a deepcopy hook ---")
    classical = GameSettings("classical", minutes=90, increment_seconds=30)
    blitz = as_blitz(classical)
    print(f"replace: {classical.minutes}+{classical.increment_seconds} -> "
          f"{blitz.minutes}+{blitz.increment_seconds}, rated still {blitz.rated}")
    session = AnalysisSession(registry.create("duel"), Engine("depth-one"))
    session.notes.append("rook lift looks strong")
    branch = copy.deepcopy(session)
    branch.board.push(("a1", "a2"))
    branch.notes.append("branch note")
    print(f"deepcopy: shared engine {branch.engine is session.engine}, notes {session.notes}")
    branch.engine.evaluate(branch.board)
    print(f"the branch's evaluation is visible on the session: {session.engine.evaluations}")

    try:
        registry.create("sicilian")
    except NotFoundError as exc:
        print(f"rejected: {exc}")


if __name__ == "__main__":
    main()
