"""Format specs, scoring rules and tournament points — the three rules that vary.

``FormatFactory`` maps a ``MatchFormat`` to a spec; ``ScoringRules`` decides how
many balls make an over and when an innings is finished; ``PointsRule`` turns a
finished match into league points.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from common import ValidationError
from lld.cricinfo.models import MatchFormat


# --8<-- [start:rules]
class ScoringRules(Protocol):
    """How a format counts. Swap it and the projector needs no edit."""

    balls_per_over: int
    wickets_per_innings: int

    def is_innings_complete(self, wickets: int, legal_balls: int, max_overs: int | None) -> bool:
        """All out, or the quota of overs bowled."""
        ...


class StandardRules:
    """Six-ball overs, ten wickets: T20, ODI and Test cricket."""

    balls_per_over = 6
    wickets_per_innings = 10

    def is_innings_complete(self, wickets: int, legal_balls: int, max_overs: int | None) -> bool:
        if wickets >= self.wickets_per_innings:
            return True
        return max_overs is not None and legal_balls >= max_overs * self.balls_per_over

    def __repr__(self) -> str:
        return "StandardRules()"


class HundredRules:
    """The Hundred: five-ball overs, 100 balls a side. Same projector, different arithmetic."""

    balls_per_over = 5
    wickets_per_innings = 10

    def is_innings_complete(self, wickets: int, legal_balls: int, max_overs: int | None) -> bool:
        if wickets >= self.wickets_per_innings:
            return True
        return max_overs is not None and legal_balls >= max_overs * self.balls_per_over

    def __repr__(self) -> str:
        return "HundredRules()"


# --8<-- [end:rules]


# --8<-- [start:format]
@dataclass(frozen=True, slots=True)
class FormatSpec:
    """Everything the services need to know about a format, in one value object."""

    format: MatchFormat
    max_overs: int | None
    innings_per_match: int
    rules: ScoringRules

    def balls_per_innings(self) -> int | None:
        return None if self.max_overs is None else self.max_overs * self.rules.balls_per_over


class FormatFactory:
    """Factory Method: adding a format is a registry entry, not a new ``if``."""

    _registry: dict[MatchFormat, tuple[int | None, int, type]] = {
        MatchFormat.T20: (20, 2, StandardRules),
        MatchFormat.ODI: (50, 2, StandardRules),
        MatchFormat.TEST: (None, 4, StandardRules),
        MatchFormat.HUNDRED: (20, 2, HundredRules),
    }

    @classmethod
    def create(cls, match_format: MatchFormat | str) -> FormatSpec:
        try:
            max_overs, innings, rules = cls._registry[MatchFormat(match_format)]
        except ValueError as exc:
            raise ValidationError(f"unknown match format: {match_format!r}") from exc
        return FormatSpec(MatchFormat(match_format), max_overs, innings, rules())


# --8<-- [end:format]


# --8<-- [start:points]
class PointsRule(Protocol):
    """League points for one finished match."""

    def points(self, won: bool, tied: bool, no_result: bool) -> int: ...


class LimitedOversPoints:
    """Two for a win, one for a tie or a washout, none for a loss."""

    WIN = 2
    SHARED = 1
    LOSS = 0

    def points(self, won: bool, tied: bool, no_result: bool) -> int:
        if won:
            return self.WIN
        if tied or no_result:
            return self.SHARED
        return self.LOSS


class TestChampionshipPoints:
    """Twelve for a win, four for a draw, six for a tie."""

    WIN = 12
    TIE = 6
    DRAW = 4
    LOSS = 0

    def points(self, won: bool, tied: bool, no_result: bool) -> int:
        if won:
            return self.WIN
        if tied:
            return self.TIE
        if no_result:
            return self.DRAW
        return self.LOSS


# --8<-- [end:points]
