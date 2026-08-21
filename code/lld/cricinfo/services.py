"""The scorer's service, the live subscribers and the tournament table.

``ScoreUpdateService`` is the only mutable object in the package. Writers take
its lock, append to the delivery log, re-run the projector and publish the new
snapshot with one assignment. Readers take no lock at all: they read an
immutable object that is either the old score or the new one, never half a ball.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Protocol

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.cricinfo.models import (
    BallType,
    Commentary,
    Delivery,
    InningsClosedError,
    MatchNotFoundError,
    MatchSnapshot,
    MatchStateError,
    MatchStatus,
    Team,
    UnknownPlayerError,
    Wicket,
)
from lld.cricinfo.projector import InningsSetup, MatchProjector, MatchSetup
from lld.cricinfo.strategies import PointsRule


# --8<-- [start:subscribers]
class ScoreSubscriber(Protocol):
    """Observer interface. Called outside the writer lock, with an immutable snapshot."""

    def on_update(self, snapshot: MatchSnapshot) -> None: ...


class LiveScoreBoard:
    """The score strip. Keeps only the latest snapshot; late joiners are not a problem."""

    def __init__(self) -> None:
        self._snapshot: MatchSnapshot | None = None

    def on_update(self, snapshot: MatchSnapshot) -> None:
        self._snapshot = snapshot

    def render(self) -> str:
        return self._snapshot.headline() if self._snapshot else "no play yet"


class CommentaryFeed:
    """Holds the projected commentary, not an append-only tail.

    That is the point: after a correction the replay produces different lines,
    and a feed that appended blindly would show the wrong ball twice.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: tuple[Commentary, ...] = ()

    def on_update(self, snapshot: MatchSnapshot) -> None:
        with self._lock:
            self._lines = snapshot.commentary

    def latest(self, count: int = 3) -> list[str]:
        with self._lock:
            return [f"{c.over_number}.{c.position_in_over} {c.text}" for c in self._lines[-count:]]


class WicketAlert:
    """Fires only when the wicket count grows — the classic filtering observer."""

    def __init__(self) -> None:
        self._seen = 0
        self.alerts: list[str] = []

    def on_update(self, snapshot: MatchSnapshot) -> None:
        wickets = snapshot.match.wickets()
        if wickets > self._seen:
            self.alerts.append(f"WICKET at {snapshot.match.summary()}")
        self._seen = wickets


# --8<-- [end:subscribers]


# --8<-- [start:service]
class ScoreUpdateService:
    """One match, one writer lock, and a snapshot readers never block on."""

    def __init__(
        self,
        setup: MatchSetup,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._setup = setup
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("d")
        self._projector = MatchProjector(setup)
        self._lock = threading.RLock()
        self._innings: list[InningsSetup] = []
        self._logs: list[list[Delivery]] = []
        self._status = MatchStatus.SCHEDULED
        self._result = ""
        self._version = 0
        self._subscribers: list[ScoreSubscriber] = []
        self._snapshot = self._project()

    # -- reads: no lock, by construction --------------------------------------
    @property
    def snapshot(self) -> MatchSnapshot:
        """Readers get the last published snapshot. Immutable, so it cannot tear."""
        return self._snapshot

    def deliveries(self, innings_number: int) -> tuple[Delivery, ...]:
        return tuple(self._log(innings_number))

    def subscribe(self, subscriber: ScoreSubscriber) -> None:
        subscriber.on_update(self._snapshot)
        self._subscribers.append(subscriber)

    # -- writes ---------------------------------------------------------------
    def start_match(self) -> MatchSnapshot:
        with self._lock:
            if self._status is not MatchStatus.SCHEDULED:
                raise MatchStateError(f"match is {self._status}, not scheduled")
            self._status = MatchStatus.LIVE
            return self._publish()

    def start_innings(self, batting_team_id: str, target: int | None = None) -> MatchSnapshot:
        with self._lock:
            # The first innings opens on a live match; every later one needs the break.
            expected = MatchStatus.INNINGS_BREAK if self._innings else MatchStatus.LIVE
            if self._status is not expected:
                raise MatchStateError(f"cannot open an innings while the match is {self._status}")
            if len(self._innings) >= self._setup.spec.innings_per_match:
                raise MatchStateError(f"a {self._setup.spec.format} match has no further innings")
            batting = self._projector.team(batting_team_id)
            bowling = self._setup.away if batting.id == self._setup.home.id else self._setup.home
            self._innings.append(
                InningsSetup(
                    number=len(self._innings) + 1,
                    batting_team_id=batting.id,
                    bowling_team_id=bowling.id,
                    max_overs=self._setup.spec.max_overs,
                    target=target,
                )
            )
            self._logs.append([])
            self._status = MatchStatus.LIVE
            return self._publish()

    def record_ball(
        self,
        striker_id: str,
        bowler_id: str,
        runs: int = 0,
        ball_type: BallType = BallType.LEGAL,
        wicket: Wicket | None = None,
        commentary: str = "",
    ) -> MatchSnapshot:
        """Append one event to the log, replay, publish. That is the whole write path."""
        with self._lock:
            setup, log = self._current()
            if self._snapshot.match.innings[setup.number - 1].closed:
                raise InningsClosedError(f"innings {setup.number} is already complete")
            self._require_squad(setup.batting_team_id, striker_id)
            self._require_squad(setup.bowling_team_id, bowler_id)
            log.append(
                Delivery(
                    id=self._ids.next_id(),
                    striker_id=striker_id,
                    bowler_id=bowler_id,
                    runs=runs,
                    ball_type=ball_type,
                    wicket=wicket,
                    commentary=commentary,
                    recorded_at=self._clock.now(),
                )
            )
            self._settle(setup)
            return self._publish()

    def undo_last_ball(self) -> MatchSnapshot:
        """Drop the last event and replay. Nothing is patched, so nothing can drift.

        Deliberately *not* routed through ``_current``: the ball a scorer most
        often needs back is the one that ended the innings, and by then the
        match is in a break rather than live. ``_settle`` reopens it.
        """
        with self._lock:
            if not self._innings:
                raise MatchStateError(f"no innings has been opened (match is {self._status})")
            setup, log = self._innings[-1], self._logs[-1]
            if not log:
                raise MatchNotFoundError(f"innings {setup.number} has no deliveries to undo")
            log.pop()
            self._settle(setup)
            return self._publish()

    def correct_ball(self, innings_number: int, index: int, corrected: Delivery) -> MatchSnapshot:
        """Replace one event in the middle of the log and replay the whole innings.

        A correction can change whether the ball was legal, which moves every
        later delivery into a different over. Only a replay can express that;
        an inverse-and-reapply cannot.
        """
        with self._lock:
            log = self._log(innings_number)
            if not 0 <= index < len(log):
                raise MatchNotFoundError(f"innings {innings_number} has no delivery at index {index}")
            log[index] = replace(corrected, id=log[index].id)  # the event id is the audit trail
            if innings_number == len(self._innings):
                self._settle(self._innings[-1])  # a correction can end -- or un-end -- the innings
            return self._publish()

    def end_innings(self) -> MatchSnapshot:
        """Declare or close the innings early."""
        with self._lock:
            setup, _ = self._current()
            self._close_innings(setup)
            return self._publish()

    def abandon(self, reason: str) -> MatchSnapshot:
        with self._lock:
            self._status = MatchStatus.ABANDONED
            self._result = reason
            return self._publish()

    # -- internals ------------------------------------------------------------
    def _current(self) -> tuple[InningsSetup, list[Delivery]]:
        if self._status is not MatchStatus.LIVE or not self._innings:
            raise MatchStateError(f"no innings is in progress (match is {self._status})")
        return self._innings[-1], self._logs[-1]

    def _log(self, innings_number: int) -> list[Delivery]:
        if not 1 <= innings_number <= len(self._logs):
            raise MatchNotFoundError(f"unknown innings {innings_number}")
        return self._logs[innings_number - 1]

    def _require_squad(self, team_id: str, player_id: str) -> None:
        team: Team = self._projector.team(team_id)
        if not team.has(player_id):
            raise UnknownPlayerError(f"{player_id} is not in {team.name}")

    def _settle(self, setup: InningsSetup) -> None:
        """Derive the match status from the replayed innings -- forwards *and* back.

        Both directions matter. An undo or a correction can take back the ball
        that ended an innings, and a status that only ever moved forward would
        leave the match sitting in a break over an innings the replay says is
        live again -- the scorecard and the status disagreeing is exactly the
        drift that event sourcing is supposed to make impossible.
        """
        match, _, _ = self._projector.project(self._innings, self._logs, self._status, self._version)
        innings = match.innings[setup.number - 1]
        if setup.target is not None and innings.runs() >= setup.target:
            self._finish(self._projector.team(setup.batting_team_id).name)
        elif innings.closed:
            self._close_innings(setup)
        elif setup.number == len(self._innings) and self._status in (
            MatchStatus.INNINGS_BREAK,
            MatchStatus.COMPLETED,
        ):
            self._reopen()

    def _reopen(self) -> None:
        """The replay says the last innings is live again, so the match is too."""
        self._status = MatchStatus.LIVE
        self._result = ""

    def _close_innings(self, setup: InningsSetup) -> None:
        if len(self._innings) >= self._setup.spec.innings_per_match:
            self._finish(self._winner_by_runs())
        else:
            self._status = MatchStatus.INNINGS_BREAK

    def _winner_by_runs(self) -> str:
        match, _, _ = self._projector.project(self._innings, self._logs, self._status, self._version)
        totals = [(i.runs(), self._projector.team(i.batting_team_id).name) for i in match.innings]
        best = max(totals)
        return "" if sum(1 for t in totals if t[0] == best[0]) > 1 else best[1]

    def _finish(self, winner: str) -> None:
        self._status = MatchStatus.COMPLETED
        self._result = f"{winner} won" if winner else "match tied"

    def _project(self) -> MatchSnapshot:
        match, cards, commentary = self._projector.project(
            self._innings, self._logs, self._status, self._version
        )
        return MatchSnapshot(self._version, match, cards, commentary, self._result)

    def _publish(self) -> MatchSnapshot:
        """The only writer of ``self._snapshot``: build first, assign last."""
        self._version += 1
        snapshot = self._project()
        self._snapshot = snapshot  # one atomic rebind; readers see old or new, never both
        for subscriber in self._subscribers:
            subscriber.on_update(snapshot)
        return snapshot


# --8<-- [end:service]


# --8<-- [start:tournament]
@dataclass(slots=True)
class PointsRow:
    team_id: str
    played: int = 0
    won: int = 0
    lost: int = 0
    drawn: int = 0
    points: int = 0


class PointsTable:
    """League standings. The points rule is injected, so a Test table is a swap."""

    def __init__(self, rule: PointsRule) -> None:
        self._rule = rule
        self._rows: dict[str, PointsRow] = {}
        self._lock = threading.Lock()

    def record(self, home_id: str, away_id: str, winner_id: str | None, no_result: bool = False) -> None:
        with self._lock:
            for team_id in (home_id, away_id):
                row = self._rows.setdefault(team_id, PointsRow(team_id))
                won = winner_id == team_id
                tied = winner_id is None and not no_result
                row.played += 1
                row.won += 1 if won else 0
                row.lost += 1 if winner_id is not None and not won else 0
                row.drawn += 1 if winner_id is None else 0
                row.points += self._rule.points(won, tied, no_result)

    def standings(self) -> list[PointsRow]:
        with self._lock:
            return sorted(self._rows.values(), key=lambda r: (-r.points, r.team_id))


@dataclass(slots=True)
class Tournament:
    name: str
    table: PointsTable


# --8<-- [end:tournament]
