"""The cricket domain: the delivery event, the composite score tree and the snapshot.

The split that matters: a ``Delivery`` is what the scorer typed, a ``Ball`` is
where the replay decided it landed. Everything from ``Ball`` upwards is a
*projection* — immutable, rebuilt from the log, never patched in place.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError


# --8<-- [start:enums]
class MatchFormat(StrEnum):
    T20 = "t20"
    ODI = "odi"
    TEST = "test"
    HUNDRED = "hundred"


class MatchStatus(StrEnum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    INNINGS_BREAK = "innings_break"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class BallType(StrEnum):
    """Each ball type answers five yes/no questions. Putting them here keeps the
    projector free of the ``if wide ... elif no_ball ...`` ladder every naive
    scorer grows."""

    LEGAL = "legal"
    WIDE = "wide"
    NO_BALL = "no_ball"
    BYE = "bye"
    LEG_BYE = "leg_bye"

    @property
    def penalty(self) -> int:
        """Automatic extra run the fielding side concedes for this delivery."""
        return 1 if self in (BallType.WIDE, BallType.NO_BALL) else 0

    @property
    def is_legal(self) -> bool:
        """Does it count towards the six balls of the over?"""
        return self not in (BallType.WIDE, BallType.NO_BALL)

    @property
    def is_faced(self) -> bool:
        """Does it count as a ball faced by the batter? A wide does not."""
        return self is not BallType.WIDE

    @property
    def credits_batter(self) -> bool:
        """Do the runs run off this delivery belong to the batter or to extras?"""
        return self in (BallType.LEGAL, BallType.NO_BALL)

    @property
    def charges_bowler(self) -> bool:
        """Byes and leg byes are not the bowler's fault."""
        return self not in (BallType.BYE, BallType.LEG_BYE)


class DismissalType(StrEnum):
    BOWLED = "bowled"
    CAUGHT = "caught"
    LBW = "lbw"
    RUN_OUT = "run_out"
    STUMPED = "stumped"
    HIT_WICKET = "hit_wicket"

    @property
    def credited_to_bowler(self) -> bool:
        return self not in (DismissalType.RUN_OUT,)


# --8<-- [end:enums]


# --8<-- [start:errors]
class MatchNotFoundError(NotFoundError):
    """Unknown match, innings or delivery index."""


class MatchStateError(InvalidStateError):
    """The match or innings status forbids the operation."""


class InningsClosedError(ConflictError):
    """The innings is already all out or has bowled its full quota."""


class UnknownPlayerError(ValidationError):
    """The striker or bowler is not in the squad for this innings."""


# --8<-- [end:errors]


# --8<-- [start:events]
@dataclass(frozen=True, slots=True)
class Wicket:
    """How a batter got out. Attached to the delivery it happened on."""

    dismissal: DismissalType
    batter_id: str
    bowler_id: str | None = None
    fielder_id: str | None = None

    def describe(self, bowler: str | None = None, fielder: str | None = None) -> str:
        """Scorecard notation: ``b Starc``, ``c Smith b Cummins``, ``run out (Jadeja)``."""
        if self.dismissal is DismissalType.RUN_OUT:
            return f"run out ({fielder})" if fielder else "run out"
        if self.dismissal is DismissalType.CAUGHT:
            return f"c {fielder or 'fielder'} b {bowler}"
        if self.dismissal is DismissalType.STUMPED:
            return f"st {fielder or 'keeper'} b {bowler}"
        if self.dismissal is DismissalType.LBW:
            return f"lbw b {bowler}"
        if self.dismissal is DismissalType.HIT_WICKET:
            return f"hit wicket b {bowler}"
        return f"b {bowler}"


@dataclass(frozen=True, slots=True)
class Delivery:
    """The event the scorer enters. It carries no position.

    Over number and ball number are *derived* by the projector, because a
    correction that turns a legal ball into a wide moves every later ball into
    a different over. Storing the position on the event would make that
    correction impossible to express.
    """

    id: str
    striker_id: str
    bowler_id: str
    runs: int = 0  # runs run off this delivery, before the type decides who gets them
    ball_type: BallType = BallType.LEGAL
    wicket: Wicket | None = None
    commentary: str = ""
    recorded_at: float = 0.0

    def __post_init__(self) -> None:
        if self.runs < 0:
            raise ValidationError("runs cannot be negative")
        if self.striker_id == self.bowler_id:
            raise ValidationError("a bowler cannot bowl to themselves")


# --8<-- [end:events]


# --8<-- [start:composite]
class ScoreNode(ABC):
    """Composite: a ball, an over, an innings and a match all answer the same
    three questions, so the scoreboard treats a leaf and the whole match alike."""

    @abstractmethod
    def runs(self) -> int: ...

    @abstractmethod
    def wickets(self) -> int: ...

    @abstractmethod
    def legal_balls(self) -> int: ...

    def summary(self) -> str:
        """Template Method: the shape of a score line is fixed, the numbers are not."""
        return f"{self.runs()}/{self.wickets()} ({self.overs_bowled()} ov)"

    def overs_bowled(self, balls_per_over: int = 6) -> str:
        completed, spare = divmod(self.legal_balls(), balls_per_over)
        return f"{completed}.{spare}"


@dataclass(frozen=True, slots=True)
class Ball(ScoreNode):
    """A positioned delivery: the leaf of the composite."""

    delivery: Delivery
    over_number: int  # 1-based
    position_in_over: int  # 1-based, counts every delivery including wides
    batter_runs: int
    extras: int

    def runs(self) -> int:
        return self.batter_runs + self.extras

    def wickets(self) -> int:
        return 1 if self.delivery.wicket else 0

    def legal_balls(self) -> int:
        return 1 if self.delivery.ball_type.is_legal else 0

    def label(self) -> str:
        kind = "" if self.delivery.ball_type is BallType.LEGAL else f" {self.delivery.ball_type}"
        return f"{self.over_number}.{self.position_in_over}{kind}: {self.runs()}"


@dataclass(frozen=True, slots=True)
class Over(ScoreNode):
    number: int
    bowler_id: str
    balls: tuple[Ball, ...]

    def runs(self) -> int:
        return sum(b.runs() for b in self.balls)

    def wickets(self) -> int:
        return sum(b.wickets() for b in self.balls)

    def legal_balls(self) -> int:
        return sum(b.legal_balls() for b in self.balls)

    def is_maiden(self, balls_per_over: int) -> bool:
        charged = sum(b.runs() for b in self.balls if b.delivery.ball_type.charges_bowler)
        return charged == 0 and self.legal_balls() == balls_per_over


@dataclass(frozen=True, slots=True)
class Innings(ScoreNode):
    number: int
    batting_team_id: str
    bowling_team_id: str
    overs: tuple[Over, ...]
    max_overs: int | None = None
    target: int | None = None
    closed: bool = False

    def runs(self) -> int:
        return sum(o.runs() for o in self.overs)

    def wickets(self) -> int:
        return sum(o.wickets() for o in self.overs)

    def legal_balls(self) -> int:
        return sum(o.legal_balls() for o in self.overs)

    def balls(self) -> tuple[Ball, ...]:
        return tuple(b for o in self.overs for b in o.balls)


@dataclass(frozen=True, slots=True)
class Match(ScoreNode):
    id: str
    format: MatchFormat
    venue: str
    home_team_id: str
    away_team_id: str
    innings: tuple[Innings, ...]
    status: MatchStatus

    def runs(self) -> int:
        return sum(i.runs() for i in self.innings)

    def wickets(self) -> int:
        return sum(i.wickets() for i in self.innings)

    def legal_balls(self) -> int:
        return sum(i.legal_balls() for i in self.innings)


# --8<-- [end:composite]


# --8<-- [start:reads]
@dataclass(frozen=True, slots=True)
class Player:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Team:
    id: str
    name: str
    players: tuple[Player, ...] = ()

    def has(self, player_id: str) -> bool:
        return any(p.id == player_id for p in self.players)

    def name_of(self, player_id: str) -> str:
        for player in self.players:
            if player.id == player_id:
                return player.name
        raise UnknownPlayerError(f"{player_id} is not in {self.name}")


@dataclass(frozen=True, slots=True)
class BattingStats:
    player_id: str
    name: str
    runs: int
    balls_faced: int
    fours: int
    sixes: int
    dismissal: str | None = None

    def strike_rate(self) -> float:
        return round(100 * self.runs / self.balls_faced, 2) if self.balls_faced else 0.0

    def line(self) -> str:
        state = self.dismissal or "not out"
        return f"{self.name} {self.runs} ({self.balls_faced}b) {state}"


@dataclass(frozen=True, slots=True)
class BowlingStats:
    player_id: str
    name: str
    legal_balls: int
    runs_conceded: int
    wickets: int
    maidens: int

    def overs(self, balls_per_over: int = 6) -> str:
        completed, spare = divmod(self.legal_balls, balls_per_over)
        return f"{completed}.{spare}"

    def economy(self, balls_per_over: int = 6) -> float:
        if not self.legal_balls:
            return 0.0
        return round(self.runs_conceded * balls_per_over / self.legal_balls, 2)

    def line(self, balls_per_over: int = 6) -> str:
        return (
            f"{self.name} {self.overs(balls_per_over)}-{self.maidens}-"
            f"{self.runs_conceded}-{self.wickets}"
        )


@dataclass(frozen=True, slots=True)
class Commentary:
    over_number: int
    position_in_over: int
    text: str
    at: float


@dataclass(frozen=True, slots=True)
class Scorecard:
    innings_number: int
    batting_team: str
    runs: int
    wickets: int
    overs: str
    extras: int
    batting: tuple[BattingStats, ...]
    bowling: tuple[BowlingStats, ...]

    def headline(self) -> str:
        return f"{self.batting_team} {self.runs}/{self.wickets} ({self.overs} ov)"


@dataclass(frozen=True, slots=True)
class MatchSnapshot:
    """What every reader sees. Immutable and published by a single assignment,
    so a reader either sees the ball or does not — never half of it."""

    version: int
    match: Match
    scorecards: tuple[Scorecard, ...]
    commentary: tuple[Commentary, ...]
    result: str = ""

    def headline(self) -> str:
        cards = " | ".join(c.headline() for c in self.scorecards)
        tail = f" - {self.result}" if self.result else ""
        return f"[v{self.version}] {cards} ({self.match.status}){tail}"


# --8<-- [end:reads]
