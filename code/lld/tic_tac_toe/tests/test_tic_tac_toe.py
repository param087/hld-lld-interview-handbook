import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import HandbookError, InvalidStateError, ValidationError
from lld.tic_tac_toe.base import GameStatus, NotYourTurnError
from lld.tic_tac_toe.models import (
    Board,
    Cell,
    CellOccupiedError,
    NothingToUndoError,
    OffBoardError,
    PlayerKind,
    Symbol,
    WinChecker,
)
from lld.tic_tac_toe.services import BoardRenderer, PlayerFactory, TicTacToeGame

LINES_3X3 = [
    [Cell(0, 0), Cell(0, 1), Cell(0, 2)],
    [Cell(1, 0), Cell(1, 1), Cell(1, 2)],
    [Cell(2, 0), Cell(2, 1), Cell(2, 2)],
    [Cell(0, 0), Cell(1, 0), Cell(2, 0)],
    [Cell(0, 1), Cell(1, 1), Cell(2, 1)],
    [Cell(0, 2), Cell(1, 2), Cell(2, 2)],
    [Cell(0, 0), Cell(1, 1), Cell(2, 2)],
    [Cell(0, 2), Cell(1, 1), Cell(2, 0)],
]


def game_with(x_moves: list[Cell], o_moves: list[Cell], size: int = 3) -> TicTacToeGame:
    return TicTacToeGame(
        [
            PlayerFactory.create(PlayerKind.HUMAN, "X", Symbol.CROSS, moves=x_moves),
            PlayerFactory.create(PlayerKind.HUMAN, "O", Symbol.NOUGHT, moves=o_moves),
        ],
        size,
    )


def empty_game(size: int = 3) -> TicTacToeGame:
    return game_with([], [], size)


def test_scripted_game_is_won_and_reported() -> None:
    game = game_with([Cell(0, 0), Cell(1, 1), Cell(2, 2)], [Cell(0, 1), Cell(0, 2)])
    renderer = BoardRenderer(game)
    result = game.play()
    assert (result.status, result.winner, result.turns) == (GameStatus.WON, "X", 5)
    assert game.board.at(Cell(1, 1)) is Symbol.CROSS
    assert "X wins" in renderer.transcript()[-1]
    assert len(game.replay()) == 6  # empty board plus one frame per move


# --8<-- [start:lines]
@pytest.mark.parametrize("line", LINES_3X3)
def test_every_row_column_and_both_diagonals_win(line: list[Cell]) -> None:
    """The anti-diagonal cases fail the moment the checker uses elif for the second diagonal."""
    elsewhere = [Cell(r, c) for r in range(3) for c in range(3) if Cell(r, c) not in line]
    game = game_with(line, elsewhere[:2])
    result = game.play()
    assert (result.winner, result.turns) == ("X", 5)


def test_centre_cell_counts_towards_both_diagonals() -> None:
    checker = WinChecker(3)
    assert checker.record(Cell(1, 1), Symbol.CROSS) is False
    assert checker.record(Cell(0, 0), Symbol.CROSS) is False
    assert checker.record(Cell(2, 2), Symbol.CROSS) is True  # main diagonal complete
    checker.erase(Cell(2, 2), Symbol.CROSS)
    assert checker.record(Cell(0, 2), Symbol.CROSS) is False
    assert checker.record(Cell(2, 0), Symbol.CROSS) is True  # anti-diagonal, same centre cell


# --8<-- [end:lines]


def test_occupied_and_off_board_cells_are_rejected() -> None:
    game = empty_game()
    game.submit_move("X", Cell(1, 1))
    with pytest.raises(CellOccupiedError):
        game.submit_move("O", Cell(1, 1))
    with pytest.raises(OffBoardError):
        game.submit_move("O", Cell(3, 0))
    assert game.turns == 1 and game.current_player == "O"  # a rejected move costs no turn


def test_out_of_turn_and_unknown_player_are_rejected() -> None:
    game = empty_game()
    with pytest.raises(NotYourTurnError):
        game.submit_move("O", Cell(0, 0))
    with pytest.raises(ValidationError):
        game.submit_move("Z", Cell(0, 0))


def test_draw_is_detected_when_the_board_fills() -> None:
    x_moves = [Cell(0, 0), Cell(0, 2), Cell(1, 0), Cell(2, 1), Cell(2, 2)]
    o_moves = [Cell(0, 1), Cell(1, 1), Cell(1, 2), Cell(2, 0)]
    result = game_with(x_moves, o_moves).play()
    assert (result.status, result.winner, result.turns) == (GameStatus.DRAWN, None, 9)


# --8<-- [start:undo]
def test_undo_after_game_over_reopens_the_game() -> None:
    game = game_with([Cell(0, 0), Cell(1, 1), Cell(2, 2)], [Cell(0, 1), Cell(0, 2)])
    game.play()
    assert game.status is GameStatus.WON

    taken_back = game.undo()

    assert taken_back.cell == Cell(2, 2)
    assert game.status is GameStatus.IN_PROGRESS  # the Memento restored the cursor
    assert game.current_player == "X" and game.turns == 4
    assert game.board.at(Cell(2, 2)) is None  # the counters were decremented too
    game.submit_move("X", Cell(2, 2))  # and the same winning move still wins
    assert game.result().winner == "X"


# --8<-- [end:undo]


def test_undo_unwinds_to_an_empty_board_then_refuses() -> None:
    game = game_with([Cell(0, 0), Cell(1, 1)], [Cell(0, 1)])
    for _ in range(3):
        game.play_turn()
    for _ in range(3):
        game.undo()
    assert game.turns == 0 and game.board.free_cells() == Board(3).free_cells()
    with pytest.raises(NothingToUndoError):
        game.undo()


# --8<-- [start:concurrency]
def test_concurrent_submissions_never_corrupt_the_board() -> None:
    game = empty_game()
    cells = [Cell(r, c) for r in range(3) for c in range(3)]

    def attempt(i: int) -> bool:
        try:
            game.submit_move("X" if i % 2 == 0 else "O", cells[i % 9])
            return True
        except HandbookError:  # lost the turn, or the cell was already taken
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = sum(pool.map(attempt, range(36)))

    history = game.history()
    assert accepted == len(history) == game.turns
    assert len({move.cell for move in history}) == len(history)  # no cell written twice
    symbols = [move.symbol for move in history]
    assert all(a is not b for a, b in zip(symbols, symbols[1:], strict=False))


# --8<-- [end:concurrency]


def test_minimax_blocks_an_immediate_threat() -> None:
    game = game_with(
        [Cell(0, 0), Cell(0, 1)],
        [],
    )
    game.submit_move("X", Cell(0, 0))
    bot = PlayerFactory.create(PlayerKind.PERFECT_BOT, "bot", Symbol.NOUGHT)
    game.submit_move("O", Cell(2, 2))
    game.submit_move("X", Cell(0, 1))  # X now threatens (0,2)
    assert bot.next_move(game.board) == Cell(0, 2)


def test_two_perfect_bots_always_draw() -> None:
    bots = TicTacToeGame(
        [
            PlayerFactory.create(PlayerKind.PERFECT_BOT, "bot-X", Symbol.CROSS),
            PlayerFactory.create(PlayerKind.PERFECT_BOT, "bot-O", Symbol.NOUGHT),
        ]
    )
    result = bots.play()
    assert (result.status, result.turns) == (GameStatus.DRAWN, 9)


def test_win_detection_scales_to_a_4x4_board() -> None:
    game = game_with(
        [Cell(0, 0), Cell(1, 1), Cell(2, 2), Cell(3, 3)],
        [Cell(0, 1), Cell(0, 2), Cell(0, 3)],
        size=4,
    )
    result = game.play()
    assert (result.winner, result.turns) == ("X", 7)


def test_random_bots_are_reproducible_and_must_be_seeded() -> None:
    def seeded_game() -> list[Cell]:
        game = TicTacToeGame(
            [
                PlayerFactory.create(PlayerKind.RANDOM_BOT, "X", Symbol.CROSS, rng=random.Random(42)),
                PlayerFactory.create(PlayerKind.RANDOM_BOT, "O", Symbol.NOUGHT, rng=random.Random(7)),
            ]
        )
        game.play()
        return [move.cell for move in game.history()]

    assert seeded_game() == seeded_game()
    with pytest.raises(ValidationError):
        PlayerFactory.create(PlayerKind.RANDOM_BOT, "X", Symbol.CROSS)


def test_finished_games_and_bad_setups_are_refused() -> None:
    game = game_with([Cell(0, 0), Cell(1, 1), Cell(2, 2)], [Cell(0, 1), Cell(0, 2)])
    game.play()
    with pytest.raises(InvalidStateError):
        game.play_turn()
    with pytest.raises(ValidationError):
        TicTacToeGame([PlayerFactory.create(PlayerKind.HUMAN, "X", Symbol.CROSS)])
    with pytest.raises(ValidationError):
        game_with([], [], size=2)
