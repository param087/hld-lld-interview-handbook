from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, Money, SequentialIdGenerator, ValidationError
from lld.bowling_alley.models import (
    Booking,
    BookingNotFoundError,
    FrameStatus,
    FrameType,
    InvalidPinCountError,
    Lane,
    LaneStatus,
    LaneUnavailableError,
)
from lld.bowling_alley.services import BowlingAlley, BowlingGame, Scoreboard
from lld.bowling_alley.strategies import HappyHourPricing, PerGamePricing
from lld.tic_tac_toe.base import GameLog, GameStatus, NotYourTurnError

TEXTBOOK = [1, 4, 4, 5, 6, 4, 5, 5, 10, 0, 1, 7, 3, 6, 4, 10, 2, 8, 6]


def solo(rolls: list[int]) -> BowlingGame:
    game = BowlingGame(["P"])
    for pins in rolls:
        game.roll("P", pins)
    return game


def alley_with(lanes: int) -> BowlingAlley:
    return BowlingAlley(
        "test",
        [Lane(f"L{i}") for i in range(1, lanes + 1)],
        clock=FakeClock(start=1_000.0),
        ids=SequentialIdGenerator("BK"),
    )


@pytest.mark.parametrize(
    ("rolls", "total"),
    [
        ([0] * 20, 0),  # a gutter game
        ([5] * 21, 150),  # every frame a spare, five pins at a time
        ([10] * 12, 300),  # the perfect game: nine strikes plus three in the tenth
        (TEXTBOOK, 133),  # the card from every bowling tutorial ever written
        ([9, 0] * 10, 90),  # nine pins and a miss, ten times over
    ],
)
def test_known_cards_score_correctly(rolls: list[int], total: int) -> None:
    game = solo(rolls)
    assert game.status is GameStatus.WON and game.total("P") == total


# --8<-- [start:provisional]
def test_a_mark_stays_provisional_until_its_bonus_balls_are_thrown() -> None:
    game = BowlingGame(["P"])
    game.roll("P", 7)
    spare = game.roll("P", 3)
    assert (spare.frame_type, spare.status, spare.running_total) == (
        FrameType.SPARE,
        FrameStatus.AWAITING_BONUS,
        10,  # the provisional total counts the bonus as zero
    )

    game.roll("P", 4)  # the next ball resolves the spare

    card = game.scorecard("P")
    assert (card[0].status, card[0].bonus, card[0].running_total) == (FrameStatus.SCORED, 4, 14)
    assert (card[1].status, card[1].running_total) == (FrameStatus.IN_PROGRESS, 18)


def test_a_strike_needs_two_bonus_balls_before_it_settles() -> None:
    game = BowlingGame(["P"])
    game.roll("P", 10)
    game.roll("P", 3)
    assert game.scorecard("P")[0].status is FrameStatus.AWAITING_BONUS

    game.roll("P", 4)

    card = game.scorecard("P")
    assert (card[0].status, card[0].running_total) == (FrameStatus.SCORED, 17)
    assert card[1].running_total == 24


# --8<-- [end:provisional]


# --8<-- [start:tenth]
@pytest.mark.parametrize(
    ("tenth", "rolls", "total"),
    [
        ([10, 10, 10], 3, 30),  # three strikes in the tenth
        ([10, 3, 4], 3, 17),  # a strike re-racks, then an ordinary pair
        ([7, 3, 5], 3, 15),  # a spare re-racks, then one bonus ball
        ([4, 5], 2, 9),  # an open tenth stops after two
    ],
)
def test_the_tenth_frame_takes_a_third_ball_only_when_it_is_earned(
    tenth: list[int], rolls: int, total: int
) -> None:
    game = solo([0] * 18 + tenth)
    frame = game.card("P")[9]
    assert len(frame.rolls) == rolls and frame.is_complete()
    assert game.total("P") == total and game.status is GameStatus.WON


# --8<-- [end:tenth]


def test_invalid_pin_counts_are_rejected_and_cost_no_turn() -> None:
    game = BowlingGame(["P"])
    with pytest.raises(InvalidPinCountError):
        game.roll("P", 11)
    with pytest.raises(InvalidPinCountError):
        game.roll("P", -1)
    game.roll("P", 7)
    with pytest.raises(InvalidPinCountError):
        game.roll("P", 4)  # only three pins are standing
    assert game.turns == 1

    tenth = solo([0] * 18 + [3])
    with pytest.raises(InvalidPinCountError):
        tenth.roll("P", 8)  # no re-rack after an ordinary first ball


def test_the_ball_passes_only_when_the_frame_closes() -> None:
    game = BowlingGame(["A", "B"])
    game.roll("A", 4)
    assert game.current_player == "A"  # an open frame needs a second ball
    game.roll("A", 3)
    assert game.current_player == "B"
    game.roll("B", 10)  # a strike ends the turn after one ball
    assert game.current_player == "A" and game.standings()[0].frame == 2
    with pytest.raises(NotYourTurnError):
        game.roll("B", 5)


def test_lane_transitions_are_guarded() -> None:
    lane = Lane("L1")
    lane.reserve("BK-1")
    with pytest.raises(LaneUnavailableError):
        lane.reserve("BK-2")  # the double booking a receptionist would otherwise make
    lane.start_play()
    with pytest.raises(LaneUnavailableError):
        lane.take_out_of_service()
    lane.release()
    assert lane.status is LaneStatus.FREE and lane.booking_id is None
    lane.take_out_of_service()
    with pytest.raises(LaneUnavailableError):
        lane.release()
    lane.return_to_service()
    assert lane.is_free()


# --8<-- [start:concurrency]
def test_concurrent_reservations_hand_out_each_lane_exactly_once() -> None:
    alley = alley_with(4)

    def book(i: int) -> str | None:
        try:
            return alley.reserve([f"p{i}"]).lane_id
        except LaneUnavailableError:  # someone else took the last lane
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(book, range(30)))

    lanes = [lane for lane in results if lane is not None]
    assert len(lanes) == 4 and len(set(lanes)) == 4  # every lane booked exactly once
    assert results.count(None) == 26 and alley.free_lanes() == 0


# --8<-- [end:concurrency]


def test_pricing_is_a_strategy_and_discounts_compose() -> None:
    booking = Booking("BK-1", "L1", ("a", "b", "c"), games=2, shoes=2, price=Money(0), created_at=0.0)
    assert PerGamePricing().quote(booking) == Money.of("45.00")  # 6.50 x 6 games + 3.00 x 2 shoes
    assert HappyHourPricing(PerGamePricing(), 20).quote(booking) == Money.of("36.00")
    with pytest.raises(ValidationError):
        HappyHourPricing(PerGamePricing(), 120)


def test_the_scoreboard_and_the_log_observe_the_same_game() -> None:
    game = BowlingGame(["A", "B"])
    board = Scoreboard(game)
    log = GameLog()
    game.subscribe(log)
    assert board.rows() == ()

    game.roll("A", 10)

    rows = board.rows()
    assert (rows[0].player, rows[0].card, rows[0].final) == ("A", "X", False)
    assert rows[1].total == 0
    assert any("A frame 1" in event.text for event in log.events())


def test_the_alley_rejects_bad_bookings_and_unknown_ids() -> None:
    alley = alley_with(1)
    with pytest.raises(ValidationError):
        alley.reserve([])
    with pytest.raises(ValidationError):
        alley.reserve([f"p{i}" for i in range(7)])
    with pytest.raises(ValidationError):
        alley.reserve(["a"], shoes=2)  # more pairs of shoes than feet

    booking = alley.reserve(["a"], games=1, shoes=1)
    with pytest.raises(BookingNotFoundError):
        alley.start_game("nope")
    started = alley.start_game(booking.id)
    assert alley.game(booking.id) is started and alley.free_lanes() == 0
    alley.finish(booking.id)
    with pytest.raises(LaneUnavailableError):
        alley.finish(booking.id)  # the lane is already back in the pool
