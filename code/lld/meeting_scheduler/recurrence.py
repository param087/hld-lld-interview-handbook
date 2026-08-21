"""Recurrence expansion (Strategy) and slot finding (Strategy), both DST-aware.

The rule that makes this correct: a recurring meeting repeats in *wall-clock* time.
Adding 24 hours to a UTC instant is wrong twice a year; adding one day to the local
naive time and re-attaching the zone is right.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from common import ValidationError
from lld.meeting_scheduler.models import (
    Frequency,
    Interval,
    RecurrenceRule,
    User,
    free_gaps,
)

MAX_OCCURRENCES = 1000  # a bound so "every day, no end date" cannot hang the expander


# --8<-- [start:localise]
def localise(naive: datetime, zone: ZoneInfo) -> datetime:
    """Attach a zone to a wall-clock time, stepping over the spring-forward gap.

    On the morning the clocks jump, 02:30 local does not exist. Python will happily
    build the value and then give it the pre-transition offset, so a round trip
    through UTC comes back as a different wall time - that is the test for a gap.
    """
    candidate = naive.replace(tzinfo=zone)
    round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_trip != naive:
        candidate = (naive + timedelta(hours=1)).replace(tzinfo=zone)
    return candidate


def shifted(first: datetime, days: int) -> datetime:
    """The same wall-clock time, ``days`` later, in the same zone."""
    zone = first.tzinfo
    if not isinstance(zone, ZoneInfo):
        return first + timedelta(days=days)
    return localise(first.replace(tzinfo=None) + timedelta(days=days), zone)


# --8<-- [end:localise]


# --8<-- [start:recurrence]
class RecurrenceStrategy(Protocol):
    """Expands a series into start times that fall inside a window."""

    def starts(self, first: datetime, window: Interval) -> Iterator[datetime]: ...


class NoRecurrence:
    def starts(self, first: datetime, window: Interval) -> Iterator[datetime]:
        if window.start <= first < window.end:
            yield first


class DailyRecurrence:
    def __init__(self, rule: RecurrenceRule) -> None:
        self._rule = rule

    def starts(self, first: datetime, window: Interval) -> Iterator[datetime]:
        rule = self._rule
        for index in range(MAX_OCCURRENCES):
            if rule.count is not None and index >= rule.count:
                return
            moment = shifted(first, index * rule.every)
            if rule.until is not None and moment > rule.until:
                return
            if moment >= window.end:
                return
            if moment >= window.start:
                yield moment


class WeeklyRecurrence:
    """``weekdays`` empty means "the weekday the series starts on"."""

    def __init__(self, rule: RecurrenceRule) -> None:
        self._rule = rule

    def starts(self, first: datetime, window: Interval) -> Iterator[datetime]:
        rule = self._rule
        weekdays = sorted(rule.weekdays) or [first.weekday()]
        emitted = 0
        for week in range(MAX_OCCURRENCES):
            monday = shifted(first, -first.weekday() + week * 7 * rule.every)
            for weekday in weekdays:
                moment = shifted(monday, weekday)
                if moment < first:
                    continue
                if rule.until is not None and moment > rule.until:
                    return
                if moment >= window.end or (rule.count is not None and emitted >= rule.count):
                    return
                emitted += 1
                if moment >= window.start:
                    yield moment


def strategy_for(rule: RecurrenceRule | None) -> RecurrenceStrategy:
    """Factory: the rule is data, the expansion is behaviour."""
    if rule is None:
        return NoRecurrence()
    if rule.frequency is Frequency.DAILY:
        return DailyRecurrence(rule)
    if rule.frequency is Frequency.WEEKLY:
        return WeeklyRecurrence(rule)
    raise ValidationError(f"unsupported recurrence frequency {rule.frequency}")


# --8<-- [end:recurrence]


# --8<-- [start:slots]
class SlotFinder(Protocol):
    """Picks an interval of ``duration`` that clears every busy block in ``window``."""

    def find(
        self,
        attendees: Sequence[User],
        busy: list[Interval],
        window: Interval,
        duration: timedelta,
    ) -> Interval | None: ...


class EarliestSlotFinder:
    """Merge the busy blocks, then take the first gap that is long enough."""

    def find(
        self,
        attendees: Sequence[User],
        busy: list[Interval],
        window: Interval,
        duration: timedelta,
    ) -> Interval | None:
        for gap in free_gaps(busy, window):
            if gap.duration >= duration:
                return Interval(gap.start, gap.start + duration)
        return None


class WorkingHoursSlotFinder:
    """Same scan, but every attendee's out-of-hours time counts as busy.

    Each attendee's working day is in *their* zone, so this is where three people on
    three continents discover they have no common hour - and the finder returns None
    instead of booking someone's 03:00.
    """

    def __init__(self, start_hour: int = 9, end_hour: int = 17) -> None:
        if not 0 <= start_hour < end_hour <= 24:
            raise ValidationError("working hours must satisfy 0 <= start < end <= 24")
        self._start_hour = start_hour
        self._end_hour = end_hour
        self._inner = EarliestSlotFinder()

    def find(
        self,
        attendees: Sequence[User],
        busy: list[Interval],
        window: Interval,
        duration: timedelta,
    ) -> Interval | None:
        blocks = list(busy)
        for attendee in attendees:
            blocks.extend(self._off_hours(attendee, window))
        return self._inner.find(attendees, blocks, window, duration)

    def _off_hours(self, attendee: User, window: Interval) -> list[Interval]:
        """Build this attendee's working days, then treat everything else as busy."""
        zone = attendee.zone
        working: list[Interval] = []
        day = window.start.astimezone(zone).date() - timedelta(days=1)
        last = window.end.astimezone(zone).date() + timedelta(days=1)
        while day <= last:
            opens = localise(datetime.combine(day, time(self._start_hour)), zone)
            closes = (
                localise(datetime.combine(day, time(self._end_hour)), zone)
                if self._end_hour < 24
                else localise(datetime.combine(day + timedelta(days=1), time(0)), zone)
            )
            working.append(Interval(opens, closes))
            day += timedelta(days=1)
        return free_gaps(working, window)


# --8<-- [end:slots]
