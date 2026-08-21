"""Prototype: a clone is an independent object, and the copy decision is made field by field."""

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError
from patterns.prototype import (
    AnalysisSession,
    Board,
    Engine,
    GameSettings,
    MoveSearch,
    Piece,
    PieceKind,
    Prototype,
    PrototypeRegistry,
    Side,
    as_blitz,
    starting_position,
)

SEARCH_DEPTH = 3


def test_clone_is_an_independent_board_that_shares_its_frozen_pieces() -> None:
    original = starting_position()
    cloned = original.clone()
    assert isinstance(original, Prototype)
    assert cloned == original and cloned is not original
    assert cloned.squares is not original.squares
    assert cloned.move_log is not original.move_log
    assert cloned.squares["a1"] is original.squares["a1"]  # frozen values are shared, not copied
    cloned.push(("a1", "a2"))
    assert "a2" in cloned.squares and "a2" not in original.squares
    assert original.move_log == [] and original.side_to_move is Side.WHITE


def test_a_shallow_copy_is_not_a_clone_but_deepcopy_and_clone_agree() -> None:
    original = starting_position()
    shallow = copy.copy(original)
    shallow.push(("b1", "b2"))
    assert original.squares is shallow.squares  # the mutation leaked back
    assert "b2" in original.squares
    assert original.side_to_move is not shallow.side_to_move  # containers shared, scalars not

    reference = starting_position()
    for candidate in (reference.clone(), copy.deepcopy(reference)):
        candidate.push(("b1", "b2"))
        assert reference == starting_position()


def test_the_registry_hands_out_copies_and_never_the_stored_prototype() -> None:
    registry: PrototypeRegistry[Board] = PrototypeRegistry()
    registry.register("duel", starting_position())
    registry.register("bare_kings", Board({"a1": Piece(PieceKind.KING, Side.WHITE)}))
    assert registry.names == ("bare_kings", "duel")

    first, second = registry.create("duel"), registry.create("duel")
    assert first == second and first is not second
    first.push(("a1", "a2"))
    assert registry.create("duel") == starting_position()  # the prototype survived


def test_registry_rejects_a_duplicate_name_and_an_unknown_name() -> None:
    registry: PrototypeRegistry[Board] = PrototypeRegistry()
    registry.register("duel", starting_position())
    with pytest.raises(ConflictError, match="already registered"):
        registry.register("duel", Board())
    with pytest.raises(NotFoundError, match="no prototype named 'sicilian'"):
        registry.create("sicilian")


@pytest.mark.parametrize("depth", [0, 1, 2, SEARCH_DEPTH])
def test_cloning_and_make_unmake_explore_the_same_tree_and_leave_the_caller_untouched(
    depth: int,
) -> None:
    board = starting_position()
    forking, in_place = MoveSearch(), MoveSearch()
    by_cloning = forking.count_by_cloning(board, depth)
    by_undo = in_place.count_in_place(board, depth)
    assert by_cloning == by_undo >= 1
    assert forking.clones >= by_cloning - 1  # one clone per edge explored
    assert in_place.clones == 0  # the whole point: no allocation per branch
    assert board == starting_position()  # both searches restored or never touched it


def test_push_validates_the_source_square_and_the_side_to_move() -> None:
    board = starting_position()
    with pytest.raises(ValidationError, match="no piece on c2"):
        board.push(("c2", "c3"))
    with pytest.raises(InvalidStateError, match="white to move, not black"):
        board.push(("d4", "d3"))
    with pytest.raises(ValidationError, match="depth cannot be negative"):
        MoveSearch().count_by_cloning(board, -1)
    assert board == starting_position()


def test_a_capture_is_restored_by_unmake() -> None:
    board = Board(
        {
            "a1": Piece(PieceKind.ROOK, Side.WHITE),
            "a2": Piece(PieceKind.ROOK, Side.BLACK),
        }
    )
    captured = board.push(("a1", "a2"))
    assert captured == Piece(PieceKind.ROOK, Side.BLACK)
    assert board.squares == {"a2": Piece(PieceKind.ROOK, Side.WHITE)}
    board.pop(("a1", "a2"), captured)
    assert board.squares["a1"].side is Side.WHITE and board.squares["a2"].side is Side.BLACK
    assert board.move_log == [] and board.side_to_move is Side.WHITE
    with pytest.raises(InvalidStateError, match="nothing on b2 to unmake"):
        board.pop(("b1", "b2"), None)


def test_deepcopy_hook_copies_the_board_and_notes_but_shares_the_engine() -> None:
    session = AnalysisSession(starting_position(), Engine("depth-one"))
    session.notes.append("original")
    branch = copy.deepcopy(session)
    assert branch.engine is session.engine  # __deepcopy__ declared the engine shared
    assert branch.board is not session.board and branch.notes is not session.notes
    branch.board.push(("a1", "a2"))
    branch.notes.append("branch")
    assert session.board == starting_position() and session.notes == ["original"]
    branch.engine.evaluate(branch.board)
    assert session.engine.evaluations == 1  # one engine, one counter


def test_dataclasses_replace_returns_a_new_value_and_catches_a_misspelled_field() -> None:
    classical = GameSettings("classical", minutes=90, increment_seconds=30)
    blitz = as_blitz(classical)
    assert blitz == GameSettings("classical", minutes=3, increment_seconds=2, rated=True)
    assert classical.minutes == 90  # frozen: the original cannot have changed
    assert blitz is not classical
    with pytest.raises(TypeError):
        replace(classical, minuts=3)


def test_many_threads_clone_one_prototype_without_disturbing_it_or_each_other() -> None:
    prototype = starting_position()

    def branch(index: int) -> str:
        board = prototype.clone()
        board.push(("a1", "a2") if index % 2 == 0 else ("b1", "c1"))
        return board.render()

    with ThreadPoolExecutor(max_workers=8) as pool:
        renders = set(pool.map(branch, range(200)))
    assert len(renders) == 2  # two distinct moves, never a half-applied third
    assert prototype == starting_position()
