import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import HandbookError, InvalidStateError, ValidationError
from lld.snake_and_ladder.models import (
    Board,
    GameConfig,
    InvalidJumpError,
    Jump,
    JumpKind,
    OvershootRule,
    Position,
)
from lld.snake_and_ladder.services import BoardFactory, SnakeAndLadderGame
from lld.snake_and_ladder.strategies import FairDice, LoadedDice, ScriptedDice
from lld.tic_tac_toe.base import GameLog, GameStatus, NotYourTurnError

SMALL = GameConfig(size=20)
MEDIUM = GameConfig(size=60)


def small_board() -> Board:
    return Board([Jump(3, 12), Jump(15, 5)], SMALL)


def test_seeded_game_on_the_classic_board_is_reproducible() -> None:
    def run() -> tuple[str | None, int, list[str]]:
        game = SnakeAndLadderGame(["Ana", "Bo", "Cy"], FairDice(random.Random(42)))
        log = GameLog()
        game.subscribe(log)
        result = game.play()
        assert len(log.events()) == result.turns + 2  # one per turn, plus start and win
        return result.winner, result.turns, game.ranking()

    assert run() == run() == ("Cy", 69, ["Cy", "Ana", "Bo"])


# --8<-- [start:scripted]
def test_a_scripted_game_takes_a_ladder_then_a_snake_then_finishes_exactly() -> None:
    game = SnakeAndLadderGame(["A", "B"], ScriptedDice([3, 2, 3, 2, 6, 2, 6, 2, 3]), small_board())
    result = game.play()
    records = game.records()

    assert records[0].jumps == (Jump(3, 12),) and records[0].jumps[0].kind is JumpKind.LADDER
    assert records[2].jumps == (Jump(15, 5),) and records[2].jumps[0].kind is JumpKind.SNAKE
    assert (result.status, result.winner, result.turns) == (GameStatus.WON, "A", 9)
    assert game.position("A") == Position(20) and game.position("B") == Position(8)


@pytest.mark.parametrize(
    ("rule", "expected"),
    [(OvershootRule.STAY, 18), (OvershootRule.BOUNCE, 17), (OvershootRule.ANY, 20)],
)
def test_the_overshoot_rule_decides_the_last_five_squares(rule: OvershootRule, expected: int) -> None:
    board = Board([], GameConfig(size=20, overshoot=rule))
    landed, jumps = board.step(Position(18), 5)  # 18 + 5 = 23, three past home
    assert (landed.square, jumps) == (expected, ())


# --8<-- [end:scripted]


# --8<-- [start:validation]
@pytest.mark.parametrize(
    ("jumps", "config", "fragment"),
    [
        ([Jump(10, 30), Jump(10, 5)], MEDIUM, "overlaps"),
        ([Jump(10, 30), Jump(30, 50)], MEDIUM, "lands on the head"),
        (
            [Jump(10, 30), Jump(30, 50), Jump(50, 10)],
            GameConfig(size=60, allow_chained_jumps=True),
            "loops back",
        ),
        ([Jump(10, 90)], MEDIUM, "leaves a board"),
        ([Jump(60, 10)], MEDIUM, "starts on the last square"),
        ([Jump(10, 10)], MEDIUM, "goes nowhere"),
    ],
)
def test_broken_boards_are_rejected_at_construction(
    jumps: list[Jump], config: GameConfig, fragment: str
) -> None:
    with pytest.raises(InvalidJumpError, match=fragment):
        Board(jumps, config)


# --8<-- [end:validation]


def test_chained_jumps_are_followed_only_when_the_config_allows_them() -> None:
    jumps = [Jump(5, 10), Jump(10, 3)]
    chained = Board(jumps, GameConfig(size=20, allow_chained_jumps=True))
    landed, taken = chained.step(Position(0), 5)
    assert (landed, taken) == (Position(3), (Jump(5, 10), Jump(10, 3)))
    with pytest.raises(InvalidJumpError):
        Board(jumps, GameConfig(size=20))


def test_out_of_turn_finished_and_undersized_games_are_rejected() -> None:
    game = SnakeAndLadderGame(["A", "B"], ScriptedDice([3, 2, 3, 2, 6, 2, 6, 2, 3]), small_board())
    with pytest.raises(NotYourTurnError):
        game.take_turn("B")
    game.play()
    with pytest.raises(InvalidStateError):
        game.play_turn()
    with pytest.raises(ValidationError):
        SnakeAndLadderGame(["solo"], ScriptedDice([1]))


def test_playing_to_last_place_skips_finishers_and_ranks_everyone() -> None:
    board = Board([], GameConfig(size=10, play_to_last=True))
    game = SnakeAndLadderGame(["A", "B", "C"], ScriptedDice([5, 5, 1, 5, 5]), board)
    result = game.play()
    assert (result.winner, result.turns) == ("A", 5)
    assert game.ranking() == ["A", "B", "C"] and game.position("C") == Position(1)


# --8<-- [start:concurrency]
def test_concurrent_turns_produce_exactly_one_record_per_turn() -> None:
    game = SnakeAndLadderGame(["A", "B", "C"], FairDice(random.Random(3)))
    accepted = 0

    def attempt(i: int) -> bool:
        try:
            game.take_turn(game.players[i % 3])
            return True
        except HandbookError:  # not this player's turn, or the game just ended
            return False

    for _ in range(20):
        if game.status not in (GameStatus.NOT_STARTED, GameStatus.IN_PROGRESS):
            break
        with ThreadPoolExecutor(max_workers=6) as pool:
            accepted += sum(pool.map(attempt, range(90)))

    records = game.records()
    assert game.status is GameStatus.WON
    assert accepted == len(records) == game.turns
    assert [r.number for r in records] == list(range(1, len(records) + 1))
    for player in game.players:  # every position is the end of that player's last turn
        own = [r for r in records if r.player == player]
        assert game.position(player) == own[-1].end
    assert game.ranking()[0] == game.result().winner


# --8<-- [end:concurrency]


def test_dice_strategies_are_seeded_bounded_and_validated() -> None:
    pair = FairDice(random.Random(42), sides=6, count=2)
    rolls = [pair.roll() for _ in range(50)]
    assert pair.max_roll == 12 and all(2 <= r <= 12 for r in rolls)
    twin = FairDice(random.Random(42), sides=6, count=2)  # same seed, same 50 rolls
    assert rolls == [twin.roll() for _ in range(50)]

    loaded = LoadedDice(random.Random(1), [0, 0, 0, 0, 0, 10])
    assert {loaded.roll() for _ in range(20)} == {6}

    with pytest.raises(InvalidStateError):
        ScriptedDice([]).roll()
    with pytest.raises(ValidationError):
        FairDice(random.Random(0), sides=1)
    with pytest.raises(ValidationError):
        LoadedDice(random.Random(0), [0, 0])


def test_random_boards_are_valid_by_construction() -> None:
    board = BoardFactory.random_board(random.Random(7), snakes=8, ladders=8)
    jumps = board.jumps
    assert len(jumps) == 16
    assert not {jump.end for jump in jumps.values()} & set(jumps)  # no jump feeds another
    with pytest.raises(ValidationError):
        BoardFactory.random_board(random.Random(7), snakes=20, ladders=20, config=GameConfig(size=20))
