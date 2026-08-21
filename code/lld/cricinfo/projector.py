"""The replay: delivery logs in, one immutable ``MatchSnapshot`` out.

``MatchProjector`` holds no state and no lock. That is deliberate — it is the
piece a correction re-runs from scratch, and a pure function is the only kind of
recompute you can trust after an edit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from lld.cricinfo.models import (
    Ball,
    BattingStats,
    BowlingStats,
    Commentary,
    Delivery,
    Innings,
    Match,
    MatchStatus,
    Over,
    Scorecard,
    Team,
    UnknownPlayerError,
)
from lld.cricinfo.strategies import FormatSpec


# --8<-- [start:setup]
@dataclass(frozen=True, slots=True)
class MatchSetup:
    """The fixed facts of a match: who, where, and under which format."""

    match_id: str
    venue: str
    home: Team
    away: Team
    spec: FormatSpec


@dataclass(frozen=True, slots=True)
class InningsSetup:
    number: int
    batting_team_id: str
    bowling_team_id: str
    max_overs: int | None = None
    target: int | None = None


# --8<-- [end:setup]


@dataclass(slots=True)
class _Batter:
    """Mutable accumulator used only inside one projection pass."""

    name: str
    runs: int = 0
    balls_faced: int = 0
    fours: int = 0
    sixes: int = 0
    dismissal: str | None = None


@dataclass(slots=True)
class _Bowler:
    name: str
    legal_balls: int = 0
    runs_conceded: int = 0
    wickets: int = 0
    maidens: int = 0


# --8<-- [start:projector]
class MatchProjector:
    """Pure projection. No state, no locks, no partial updates."""

    def __init__(self, setup: MatchSetup) -> None:
        self._setup = setup
        self._rules = setup.spec.rules
        self._teams = {setup.home.id: setup.home, setup.away.id: setup.away}

    def team(self, team_id: str) -> Team:
        try:
            return self._teams[team_id]
        except KeyError:
            raise UnknownPlayerError(f"{team_id} is not playing this match") from None

    def project(
        self,
        innings_setups: Sequence[InningsSetup],
        logs: Sequence[Sequence[Delivery]],
        status: MatchStatus,
        version: int,
    ) -> tuple[Match, tuple[Scorecard, ...], tuple[Commentary, ...]]:
        innings: list[Innings] = []
        cards: list[Scorecard] = []
        commentary: list[Commentary] = []
        for setup, log in zip(innings_setups, logs, strict=True):
            one, card, lines = self._project_innings(setup, log)
            innings.append(one)
            cards.append(card)
            commentary.extend(lines)
        match = Match(
            id=self._setup.match_id,
            format=self._setup.spec.format,
            venue=self._setup.venue,
            home_team_id=self._setup.home.id,
            away_team_id=self._setup.away.id,
            innings=tuple(innings),
            status=status,
        )
        return match, tuple(cards), tuple(commentary)

    def _project_innings(
        self, setup: InningsSetup, log: Sequence[Delivery]
    ) -> tuple[Innings, Scorecard, list[Commentary]]:
        batting_team, bowling_team = self.team(setup.batting_team_id), self.team(setup.bowling_team_id)
        batters: dict[str, _Batter] = {}
        bowlers: dict[str, _Bowler] = {}
        overs: list[Over] = []
        current: list[Ball] = []
        commentary: list[Commentary] = []
        over_number = 0
        legal_in_over = 0
        extras_total = 0

        for delivery in log:
            kind = delivery.ball_type
            if not current:
                over_number += 1
            batter_runs = delivery.runs if kind.credits_batter else 0
            extras = kind.penalty + (0 if kind.credits_batter else delivery.runs)
            ball = Ball(delivery, over_number, len(current) + 1, batter_runs, extras)
            current.append(ball)
            extras_total += extras

            batter = self._batter(batters, batting_team, delivery.striker_id)
            batter.runs += batter_runs
            batter.balls_faced += 1 if kind.is_faced else 0
            batter.fours += 1 if kind.credits_batter and delivery.runs == 4 else 0
            batter.sixes += 1 if kind.credits_batter and delivery.runs == 6 else 0

            bowler = self._bowler(bowlers, bowling_team, delivery.bowler_id)
            bowler.legal_balls += 1 if kind.is_legal else 0
            bowler.runs_conceded += ball.runs() if kind.charges_bowler else 0

            if delivery.wicket is not None:
                out = self._batter(batters, batting_team, delivery.wicket.batter_id)
                out.dismissal = delivery.wicket.describe(
                    self._name(bowling_team, delivery.wicket.bowler_id),
                    self._name(bowling_team, delivery.wicket.fielder_id),
                )
                if delivery.wicket.dismissal.credited_to_bowler:
                    bowler.wickets += 1
            if delivery.commentary:
                commentary.append(
                    Commentary(over_number, len(current), delivery.commentary, delivery.recorded_at)
                )

            legal_in_over += 1 if kind.is_legal else 0
            if legal_in_over == self._rules.balls_per_over:
                overs.append(self._close_over(over_number, current, bowlers))
                current, legal_in_over = [], 0
        if current:
            overs.append(self._close_over(over_number, current, bowlers))

        innings = Innings(
            number=setup.number,
            batting_team_id=setup.batting_team_id,
            bowling_team_id=setup.bowling_team_id,
            overs=tuple(overs),
            max_overs=setup.max_overs,
            target=setup.target,
            closed=self._rules.is_innings_complete(
                sum(o.wickets() for o in overs),
                sum(o.legal_balls() for o in overs),
                setup.max_overs,
            ),
        )
        card = Scorecard(
            innings_number=setup.number,
            batting_team=batting_team.name,
            runs=innings.runs(),
            wickets=innings.wickets(),
            overs=innings.overs_bowled(self._rules.balls_per_over),
            extras=extras_total,
            batting=tuple(
                BattingStats(pid, b.name, b.runs, b.balls_faced, b.fours, b.sixes, b.dismissal)
                for pid, b in batters.items()
            ),
            bowling=tuple(
                BowlingStats(pid, w.name, w.legal_balls, w.runs_conceded, w.wickets, w.maidens)
                for pid, w in bowlers.items()
            ),
        )
        return innings, card, commentary

    def _close_over(self, number: int, balls: list[Ball], bowlers: dict[str, _Bowler]) -> Over:
        over = Over(number, balls[0].delivery.bowler_id, tuple(balls))
        if over.is_maiden(self._rules.balls_per_over):
            bowlers[over.bowler_id].maidens += 1
        return over

    @staticmethod
    def _name(team: Team, player_id: str | None) -> str | None:
        return team.name_of(player_id) if player_id else None

    @staticmethod
    def _batter(batters: dict[str, _Batter], team: Team, player_id: str) -> _Batter:
        if player_id not in batters:
            batters[player_id] = _Batter(team.name_of(player_id))
        return batters[player_id]

    @staticmethod
    def _bowler(bowlers: dict[str, _Bowler], team: Team, player_id: str) -> _Bowler:
        if player_id not in bowlers:
            bowlers[player_id] = _Bowler(team.name_of(player_id))
        return bowlers[player_id]


# --8<-- [end:projector]
