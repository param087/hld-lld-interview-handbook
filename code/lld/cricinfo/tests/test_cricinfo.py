from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from common import FakeClock, SequentialIdGenerator, ValidationError
from lld.cricinfo.models import (
    BallType,
    DismissalType,
    MatchFormat,
    MatchStateError,
    MatchStatus,
    Player,
    Team,
    UnknownPlayerError,
    Wicket,
)
from lld.cricinfo.projector import MatchSetup
from lld.cricinfo.services import LiveScoreBoard, PointsTable, ScoreUpdateService, WicketAlert
from lld.cricinfo.strategies import (
    FormatFactory,
    FormatSpec,
    HundredRules,
    LimitedOversPoints,
    StandardRules,
    TestChampionshipPoints,
)

INDIA = Team("IND", "India", (Player("rohit", "Rohit"), Player("kohli", "Kohli")))
AUSTRALIA = Team("AUS", "Australia", (Player("starc", "Starc"), Player("cummins", "Cummins")))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000)


def make_service(clock: FakeClock, spec: FormatSpec | None = None) -> ScoreUpdateService:
    setup = MatchSetup("m-1", "Wankhede", INDIA, AUSTRALIA, spec or FormatFactory.create(MatchFormat.T20))
    service = ScoreUpdateService(setup, clock=clock, ids=SequentialIdGenerator("d"))
    service.start_match()
    service.start_innings("IND")
    return service


def test_scoring_an_over_projects_runs_extras_and_bowling_figures(clock: FakeClock) -> None:
    service = make_service(clock)
    for runs, kind in [(4, BallType.LEGAL), (1, BallType.LEGAL), (0, BallType.WIDE)]:
        service.record_ball("rohit", "starc", runs, kind)
    service.record_ball("kohli", "starc", 2, BallType.LEG_BYE)
    service.record_ball(
        "kohli", "starc", 0, BallType.LEGAL, Wicket(DismissalType.BOWLED, "kohli", "starc")
    )

    card = service.snapshot.scorecards[0]
    assert (card.runs, card.wickets, card.overs, card.extras) == (8, 1, "0.4", 3)
    assert card.batting[0].line() == "Rohit 5 (2b) not out"
    assert card.batting[1].line() == "Kohli 0 (2b) b Starc"
    assert card.bowling[0].line() == "Starc 0.4-0-6-1"  # the two leg byes are not charged


@pytest.mark.parametrize(
    ("kind", "runs", "expected_total", "expected_extras", "expected_legal"),
    [
        (BallType.LEGAL, 4, 4, 0, 1),
        (BallType.WIDE, 2, 3, 3, 0),  # 1 penalty plus 2 run, all extras, over does not advance
        (BallType.NO_BALL, 6, 7, 1, 0),  # the six belongs to the batter, the penalty does not
        (BallType.BYE, 3, 3, 3, 1),
        (BallType.LEG_BYE, 1, 1, 1, 1),
    ],
)
def test_ball_type_decides_who_gets_the_runs(
    clock: FakeClock,
    kind: BallType,
    runs: int,
    expected_total: int,
    expected_extras: int,
    expected_legal: int,
) -> None:
    service = make_service(clock)
    service.record_ball("rohit", "starc", runs, kind)
    innings = service.snapshot.match.innings[0]
    card = service.snapshot.scorecards[0]
    assert (innings.runs(), card.extras, innings.legal_balls()) == (
        expected_total,
        expected_extras,
        expected_legal,
    )


def test_validation_rejects_unknown_players_and_impossible_deliveries(clock: FakeClock) -> None:
    service = make_service(clock)
    with pytest.raises(UnknownPlayerError):
        service.record_ball("smith", "starc", 1)  # not in the batting squad
    with pytest.raises(UnknownPlayerError):
        service.record_ball("rohit", "kohli", 1)  # bowling for the wrong side
    with pytest.raises(ValidationError):
        service.record_ball("rohit", "starc", -1)
    with pytest.raises(MatchStateError):
        service.start_innings("IND")  # second innings needs the first to close first
    assert service.snapshot.match.innings[0].runs() == 0


def test_match_status_walks_scheduled_live_break_completed(clock: FakeClock) -> None:
    one_over = FormatSpec(MatchFormat.T20, max_overs=1, innings_per_match=2, rules=StandardRules())
    setup = MatchSetup("m-2", "Eden", INDIA, AUSTRALIA, one_over)
    service = ScoreUpdateService(setup, clock=clock, ids=SequentialIdGenerator("d"))
    assert service.snapshot.match.status is MatchStatus.SCHEDULED

    service.start_match()
    service.start_innings("IND")
    for _ in range(6):
        service.record_ball("rohit", "starc", 1)
    assert service.snapshot.match.status is MatchStatus.INNINGS_BREAK

    service.start_innings("AUS", target=7)
    for _ in range(4):  # 4 x 2 = 8 runs passes the target of 7
        service.record_ball("starc", "rohit", 2)
    assert service.snapshot.match.status is MatchStatus.COMPLETED
    assert service.snapshot.result == "Australia won"
    with pytest.raises(MatchStateError):
        service.record_ball("starc", "rohit", 1)


# --8<-- [start:correction]
def test_correcting_a_wide_moves_every_later_ball_into_a_different_over(clock: FakeClock) -> None:
    """The reason the log stores events, not positions: a correction re-cuts the overs."""
    service = make_service(clock)
    for _ in range(2):
        service.record_ball("rohit", "starc", 1)
    service.record_ball("kohli", "starc", 0, BallType.WIDE)  # index 2: the mis-entry
    for _ in range(5):
        service.record_ball("kohli", "starc", 1)

    before = service.snapshot
    assert [len(o.balls) for o in before.match.innings[0].overs] == [7, 1]
    assert (before.match.innings[0].runs(), before.scorecards[0].overs) == (8, "1.1")

    wrong = service.deliveries(1)[2]
    service.correct_ball(1, 2, replace(wrong, ball_type=BallType.LEGAL, runs=4))
    after = service.snapshot

    assert [len(o.balls) for o in after.match.innings[0].overs] == [6, 2]  # the ball moved over
    assert (after.match.innings[0].runs(), after.scorecards[0].overs) == (11, "1.2")
    assert after.version > before.version
    assert before.match.innings[0].runs() == 8  # the old snapshot is untouched


# --8<-- [end:correction]


def test_undo_removes_the_last_delivery_and_its_wicket(clock: FakeClock) -> None:
    service = make_service(clock)
    alert = WicketAlert()
    service.subscribe(alert)
    service.record_ball("rohit", "starc", 4)
    service.record_ball(
        "rohit", "starc", 0, BallType.LEGAL, Wicket(DismissalType.BOWLED, "rohit", "starc")
    )
    assert (service.snapshot.match.wickets(), len(alert.alerts)) == (1, 1)

    service.undo_last_ball()
    assert service.snapshot.match.wickets() == 0
    assert service.snapshot.scorecards[0].batting[0].dismissal is None
    assert len(service.deliveries(1)) == 1

    service.undo_last_ball()
    with pytest.raises(Exception, match="no deliveries to undo"):
        service.undo_last_ball()


# --8<-- [start:concurrency]
def test_readers_never_see_a_torn_snapshot_while_the_scorer_writes(clock: FakeClock) -> None:
    """Four scorers, four readers, one match: every snapshot a reader sees is whole."""
    service = make_service(clock)
    balls = 40

    def score(_: int) -> None:
        service.record_ball("rohit", "starc", 1)

    def read(_: int) -> bool:
        innings = service.snapshot.match.innings[0]
        # A torn read would show runs that do not match the balls the snapshot holds.
        return innings.runs() == len(innings.balls())

    with ThreadPoolExecutor(max_workers=8) as pool:
        writers = pool.map(score, range(balls))
        readers = list(pool.map(read, range(200)))
        list(writers)

    assert all(readers)
    assert len(service.deliveries(1)) == balls
    assert service.snapshot.match.innings[0].runs() == balls
    assert service.snapshot.version == balls + 2  # start_match + start_innings + one per ball


# --8<-- [end:concurrency]


def test_swapping_the_scoring_rules_changes_the_length_of_an_over(clock: FakeClock) -> None:
    hundred = FormatSpec(MatchFormat.HUNDRED, max_overs=2, innings_per_match=2, rules=HundredRules())
    service = make_service(clock, hundred)
    for _ in range(6):
        service.record_ball("rohit", "starc", 1)
    assert [len(o.balls) for o in service.snapshot.match.innings[0].overs] == [5, 1]
    assert service.snapshot.scorecards[0].overs == "1.1"

    board = LiveScoreBoard()
    service.subscribe(board)
    assert board.render().endswith("(live)")


@pytest.mark.parametrize(
    ("rule", "expected"),
    [(LimitedOversPoints(), [("IND", 2), ("AUS", 0)]), (TestChampionshipPoints(), [("IND", 12), ("AUS", 0)])],
)
def test_points_table_ranks_by_the_injected_rule(rule: object, expected: list[tuple[str, int]]) -> None:
    table = PointsTable(rule)
    table.record("IND", "AUS", winner_id="IND")
    assert [(row.team_id, row.points) for row in table.standings()] == expected
