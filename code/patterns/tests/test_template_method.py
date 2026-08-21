"""Template Method: the base class owns the order of the steps, the subclasses own the steps."""

from __future__ import annotations

import random
from collections.abc import Callable
from functools import partial

import pytest

from common import InvalidStateError, ValidationError
from patterns.template_method import (
    JUMPS,
    BoardGame,
    GameResult,
    SnakeAndLadder,
    TicTacToe,
    play_turns,
    snake_and_ladder_with_closures,
)


def seeded_die(seed: int = 42) -> Callable[[], int]:
    return partial(random.Random(seed).randint, 1, 6)


def test_tic_tac_toe_plays_a_scripted_win_through_the_shared_skeleton() -> None:
    game = TicTacToe(moves=[4, 0, 2, 6, 5, 1, 8])
    assert game.play() == GameResult(winner="X", turns=7)
    assert game.rows() == ["OOX", " XX", "O X"]
    assert game.log == ["X -> 4", "O -> 0", "X -> 2", "O -> 6", "X -> 5", "O -> 1", "X -> 8"]


def test_a_full_board_with_no_line_is_a_draw() -> None:
    result = TicTacToe(moves=[0, 1, 2, 4, 3, 5, 7, 6, 8]).play()
    assert result == GameResult(winner=None, turns=9)


@pytest.mark.parametrize(
    ("moves", "error", "message"),
    [
        ([4, 4], ValidationError, "cell 4 is taken"),
        ([9], ValidationError, "off the board"),
        ([0, 1], InvalidStateError, "no scripted move left"),
    ],
)
def test_a_step_that_rejects_a_move_stops_the_game_with_its_own_error(
    moves: list[int], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        TicTacToe(moves=moves).play()


def test_the_skeleton_calls_the_steps_and_hooks_in_a_fixed_order() -> None:
    calls: list[str] = []

    class OneTurnGame(BoardGame[str]):
        def setup(self) -> None:
            calls.append("setup")

        def choose_move(self, player: str) -> str:
            calls.append(f"choose_move({player})")
            return "m"

        def apply_move(self, player: str, move: str) -> None:
            calls.append(f"apply_move({player}, {move})")

        def after_move(self, player: str, move: str) -> None:
            calls.append("after_move")

        def is_over(self) -> bool:
            calls.append("is_over")
            return "after_move" in calls

        def winner(self) -> str | None:
            calls.append("winner")
            return "a"

    assert OneTurnGame(["a", "b"]).play() == GameResult("a", 1)
    assert calls == [
        "setup",
        "is_over",
        "choose_move(a)",
        "apply_move(a, m)",
        "after_move",
        "is_over",
        "winner",
    ]


def test_the_turn_limit_in_the_skeleton_stops_a_game_that_cannot_end() -> None:
    stuck = SnakeAndLadder(["a", "b"], roll=lambda: 6, jumps={}, size=5)
    stuck.turn_limit = 20
    with pytest.raises(InvalidStateError, match="no result after 20 turns"):
        stuck.play()  # every roll overshoots square 5, so nobody ever moves


def test_snake_and_ladder_applies_jumps_and_the_exact_finish_rule() -> None:
    rolls = iter([4, 2, 6, 3, 6, 5])  # a: 4 -> ladder to 14, b: 2; a: 20 -> ladder to 38 ...
    game = SnakeAndLadder(["a", "b"], roll=lambda: next(rolls), jumps={4: 14, 20: 38, 39: 3}, size=40)
    game.setup()
    game.apply_move("a", game.choose_move("a"))
    assert game.positions["a"] == 14
    game.apply_move("b", game.choose_move("b"))
    game.apply_move("a", game.choose_move("a"))
    assert game.positions["a"] == 38
    game.apply_move("b", game.choose_move("b"))
    game.apply_move("a", game.choose_move("a"))  # 38 + 6 overshoots 40: stays put
    assert game.positions["a"] == 38
    game.apply_move("b", game.choose_move("b"))
    assert game.positions["b"] == 10


def test_a_seeded_game_is_deterministic_and_the_closure_form_plays_the_same_game() -> None:
    players = ["Ann", "Bob"]
    first = SnakeAndLadder(players, roll=seeded_die(), jumps=JUMPS).play()
    second = SnakeAndLadder(players, roll=seeded_die(), jumps=JUMPS).play()
    assert first == second
    assert first.winner in players
    assert snake_and_ladder_with_closures(players, roll=seeded_die(), jumps=JUMPS) == first


def test_play_turns_rotates_players_and_enforces_the_limit() -> None:
    seen: list[str] = []
    result = play_turns(["a", "b", "c"], seen.append, is_over=lambda: len(seen) == 4, winner=lambda: seen[-1])
    assert result == GameResult("a", 4)
    assert seen == ["a", "b", "c", "a"]
    with pytest.raises(InvalidStateError):
        play_turns(["a", "b"], seen.append, is_over=lambda: False, winner=lambda: None, turn_limit=3)


@pytest.mark.parametrize(
    ("players", "jumps", "message"),
    [
        (["solo"], {}, "at least two players"),
        (["a", "a"], {}, "unique"),
        (["a", "b"], {0: 5}, "off the board"),
        (["a", "b"], {10: 10}, "off the board"),
        (["a", "b"], {3: 9, 9: 20}, "chains into another jump"),
    ],
)
def test_constructors_validate_players_and_jumps(players: list[str], jumps: dict[int, int], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SnakeAndLadder(players, roll=lambda: 1, jumps=jumps, size=10)


def test_the_template_cannot_be_played_without_its_steps() -> None:
    with pytest.raises(TypeError):
        BoardGame(["a", "b"])  # type: ignore[abstract]
