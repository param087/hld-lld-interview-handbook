"""A T20 over, scored ball by ball, then a mis-entered wide corrected by replay."""

from dataclasses import replace

from common import FakeClock, SequentialIdGenerator
from lld.cricinfo.models import BallType, DismissalType, MatchFormat, Player, Team, Wicket
from lld.cricinfo.projector import MatchSetup
from lld.cricinfo.services import (
    CommentaryFeed,
    LiveScoreBoard,
    PointsTable,
    ScoreUpdateService,
    WicketAlert,
)
from lld.cricinfo.strategies import FormatFactory, LimitedOversPoints

SCRIPT = [
    ("rohit", "starc", 4, BallType.LEGAL, None, "driven through cover"),
    ("rohit", "starc", 1, BallType.LEGAL, None, ""),
    ("kohli", "starc", 0, BallType.WIDE, None, "down the leg side"),
    ("kohli", "starc", 6, BallType.LEGAL, None, "over midwicket"),
    ("kohli", "starc", 2, BallType.LEG_BYE, None, ""),
    ("kohli", "starc", 0, BallType.LEGAL, None, ""),
    ("kohli", "starc", 1, BallType.LEGAL, None, ""),
    ("rohit", "cummins", 0, BallType.LEGAL, Wicket(DismissalType.BOWLED, "rohit", "cummins"), "timber"),
]


def build_service(clock: FakeClock) -> ScoreUpdateService:
    india = Team("IND", "India", (Player("rohit", "Rohit"), Player("kohli", "Kohli")))
    australia = Team("AUS", "Australia", (Player("starc", "Starc"), Player("cummins", "Cummins")))
    setup = MatchSetup("m-1", "Wankhede", india, australia, FormatFactory.create(MatchFormat.T20))
    return ScoreUpdateService(setup, clock=clock, ids=SequentialIdGenerator("d"))


def shape(service: ScoreUpdateService) -> list[int]:
    """How many deliveries landed in each over, after the latest replay."""
    return [len(over.balls) for over in service.snapshot.match.innings[0].overs]


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    service = build_service(clock)
    board, feed, alert = LiveScoreBoard(), CommentaryFeed(), WicketAlert()
    for subscriber in (board, feed, alert):
        service.subscribe(subscriber)

    service.start_match()
    service.start_innings("IND")
    for striker, bowler, runs, kind, wicket, text in SCRIPT:
        clock.advance(30)
        service.record_ball(striker, bowler, runs, kind, wicket, text)
    print(f"live: {board.render()}")
    print(f"overs hold {shape(service)} deliveries; extras {service.snapshot.scorecards[0].extras}")
    print(f"commentary: {feed.latest(2)}")
    print(f"alerts: {alert.alerts}")

    reader = service.snapshot  # a reader holding this snapshot while the scorer edits
    wrong = service.deliveries(1)[2]
    print(f"delivery 3 was entered as a {wrong.ball_type}; it was a legal ball edged for four")
    service.correct_ball(1, 2, replace(wrong, ball_type=BallType.LEGAL, runs=4, commentary="edged for four"))
    print(f"after replay: {service.snapshot.headline()}")
    print(f"overs now hold {shape(service)} deliveries; extras {service.snapshot.scorecards[0].extras}")
    print(f"the reader still holds v{reader.version}: {reader.scorecards[0].headline()}")

    card = service.snapshot.scorecards[0]
    for batting in card.batting:
        print(f"bat: {batting.line()}")
    for bowling in card.bowling:
        print(f"bowl: {bowling.line()}")

    service.undo_last_ball()
    print(f"undo the wicket: {service.snapshot.headline()}")

    table = PointsTable(LimitedOversPoints())
    table.record("IND", "AUS", winner_id="IND")
    print(f"points: {[(r.team_id, r.points) for r in table.standings()]}")


if __name__ == "__main__":
    main()
