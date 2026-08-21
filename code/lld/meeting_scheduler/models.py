"""Intervals, meetings, invitations and the builder that assembles them.

Every datetime in this package is timezone-aware. Wall-clock time is kept in the
organiser's zone because that is what a recurrence means ("09:00 every weekday",
not "08:00 UTC every weekday"), and every comparison happens in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from common import ConflictError, IdGenerator, InvalidStateError, NotFoundError, ValidationError


# --8<-- [start:enums]
class Frequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


class MeetingStatus(StrEnum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"


class InvitationStatus(StrEnum):
    PENDING = "pending"  # invited, no answer yet - still counts as busy
    ACCEPTED = "accepted"
    TENTATIVE = "tentative"  # busy, but movable
    DECLINED = "declined"  # not busy
    WITHDRAWN = "withdrawn"  # the meeting was cancelled

    @property
    def blocks_time(self) -> bool:
        return self in (InvitationStatus.PENDING, InvitationStatus.ACCEPTED, InvitationStatus.TENTATIVE)


class SlotConflictError(ConflictError):
    """The room or an attendee is already busy for the requested interval."""


class NoFreeSlotError(ConflictError):
    """No gap long enough exists in the search window."""


class InvitationStateError(InvalidStateError):
    """The invitation cannot move to that status (answering a withdrawn invitation)."""


class MeetingNotFoundError(NotFoundError):
    """Unknown meeting, room or user id."""


# --8<-- [end:enums]


# --8<-- [start:interval]
@dataclass(frozen=True, slots=True, order=True)
class Interval:
    """A half-open range ``[start, end)``. Back-to-back meetings do not overlap."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for moment in (self.start, self.end):
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValidationError(f"interval bounds must be timezone-aware: {moment!r}")
        if self.end <= self.start:
            raise ValidationError(f"interval must be positive: {self.start} to {self.end}")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, other: Interval) -> bool:
        """Half-open comparison: ``10:00-11:00`` and ``11:00-12:00`` are *not* a clash."""
        return self.start < other.end and other.start < self.end

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def clipped_to(self, window: Interval) -> Interval | None:
        start, end = max(self.start, window.start), min(self.end, window.end)
        return Interval(start, end) if start < end else None

    def utc(self) -> Interval:
        return Interval(self.start.astimezone(UTC), self.end.astimezone(UTC))

    def __str__(self) -> str:
        utc = self.utc()
        return f"{utc.start:%Y-%m-%d %H:%M}-{utc.end:%H:%M}Z"


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Sort by start, then fold anything that touches or overlaps into one block."""
    merged: list[Interval] = []
    for interval in sorted(interval.utc() for interval in intervals):
        if merged and interval.start <= merged[-1].end:
            merged[-1] = Interval(merged[-1].start, max(merged[-1].end, interval.end))
        else:
            merged.append(interval)
    return merged


def free_gaps(busy: list[Interval], window: Interval) -> list[Interval]:
    """The parts of ``window`` no busy block covers. Merging first is what makes it O(n log n)."""
    bounds = window.utc()
    gaps: list[Interval] = []
    cursor = bounds.start
    for block in merge_intervals(busy):
        if block.start > cursor:
            gaps.append(Interval(cursor, min(block.start, bounds.end)))
        cursor = max(cursor, block.end)
        if cursor >= bounds.end:
            return gaps
    gaps.append(Interval(cursor, bounds.end))
    return gaps


# --8<-- [end:interval]


# --8<-- [start:entities]
@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    timezone: str = "UTC"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class MeetingRoom:
    id: str
    name: str
    capacity: int


@dataclass(frozen=True, slots=True)
class Reminder:
    """A reminder that is due now for one occurrence of a meeting."""

    meeting_id: str
    occurrence: Interval
    minutes_before: int

    @property
    def fires_at(self) -> datetime:
        return self.occurrence.start - timedelta(minutes=self.minutes_before)


@dataclass(frozen=True, slots=True)
class RoomBooking:
    room_id: str
    meeting_id: str
    interval: Interval


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    """``every`` counts periods: DAILY/1 is every day, WEEKLY/2 is every other week."""

    frequency: Frequency
    every: int = 1
    count: int | None = None
    until: datetime | None = None
    weekdays: tuple[int, ...] = ()  # 0 = Monday, only used by WEEKLY

    def __post_init__(self) -> None:
        if self.every < 1:
            raise ValidationError("recurrence interval must be at least 1")
        if self.count is not None and self.count < 1:
            raise ValidationError("recurrence count must be at least 1")
        if any(day not in range(7) for day in self.weekdays):
            raise ValidationError("weekdays must be 0 (Monday) to 6 (Sunday)")


@dataclass(slots=True)
class Invitation:
    meeting_id: str
    attendee_id: str
    status: InvitationStatus = InvitationStatus.PENDING
    responded_at: float | None = None

    def respond(self, status: InvitationStatus, at: float) -> None:
        if self.status is InvitationStatus.WITHDRAWN:
            raise InvitationStateError(f"invitation for {self.meeting_id} was withdrawn")
        if status is InvitationStatus.WITHDRAWN:
            raise InvitationStateError("use MeetingScheduler.cancel to withdraw an invitation")
        self.status = status
        self.responded_at = at

    def withdraw(self) -> None:
        self.status = InvitationStatus.WITHDRAWN


@dataclass(slots=True)
class Meeting:
    """One series. ``start`` carries the organiser's zone, so recurrence is wall-clock."""

    id: str
    title: str
    organizer_id: str
    attendee_ids: tuple[str, ...]
    start: datetime
    duration: timedelta
    recurrence: RecurrenceRule | None = None
    room_id: str | None = None
    status: MeetingStatus = MeetingStatus.SCHEDULED
    invitations: dict[str, Invitation] = field(default_factory=dict)
    cancelled_dates: set[date] = field(default_factory=set)
    moved_dates: dict[date, datetime] = field(default_factory=dict)

    @property
    def first_interval(self) -> Interval:
        return Interval(self.start, self.start + self.duration)

    def is_active(self) -> bool:
        return self.status is MeetingStatus.SCHEDULED

    def blocks(self, attendee_id: str) -> bool:
        invitation = self.invitations.get(attendee_id)
        return self.is_active() and invitation is not None and invitation.status.blocks_time


# --8<-- [end:entities]


# --8<-- [start:builder]
class MeetingBuilder:
    """Builder: a meeting has eight fields, four of them optional and interdependent.

    Every rule lives in ``build``, so there is exactly one place where an invalid
    meeting can be born - and the fluent calls stay free of validation noise.
    """

    def __init__(self, ids: IdGenerator) -> None:
        self._ids = ids
        self._title = ""
        self._organizer: User | None = None
        self._start: datetime | None = None
        self._duration: timedelta | None = None
        self._attendees: list[str] = []
        self._recurrence: RecurrenceRule | None = None
        self._room_id: str | None = None

    def titled(self, title: str) -> MeetingBuilder:
        self._title = title.strip()
        return self

    def organized_by(self, organizer: User) -> MeetingBuilder:
        self._organizer = organizer
        return self

    def starting_at(self, local: datetime | str, zone: str | None = None) -> MeetingBuilder:
        """Local wall-clock time. Without a zone the organiser's own zone is used."""
        moment = datetime.fromisoformat(local) if isinstance(local, str) else local
        if moment.tzinfo is None:
            if zone is None and self._organizer is None:
                raise ValidationError("set the organiser or pass a zone before the start time")
            key = zone or (self._organizer.timezone if self._organizer else "UTC")
            moment = moment.replace(tzinfo=ZoneInfo(key))
        self._start = moment
        return self

    def lasting(self, minutes: int) -> MeetingBuilder:
        self._duration = timedelta(minutes=minutes)
        return self

    def with_attendees(self, *user_ids: str) -> MeetingBuilder:
        self._attendees.extend(user_ids)
        return self

    def repeating(self, rule: RecurrenceRule) -> MeetingBuilder:
        self._recurrence = rule
        return self

    def in_room(self, room_id: str | None) -> MeetingBuilder:
        self._room_id = room_id
        return self

    def build(self) -> Meeting:
        if not self._title:
            raise ValidationError("a meeting needs a title")
        if self._organizer is None:
            raise ValidationError("a meeting needs an organiser")
        if self._start is None or self._duration is None:
            raise ValidationError("a meeting needs a start time and a duration")
        if self._duration <= timedelta(0):
            raise ValidationError("a meeting must last longer than zero minutes")
        attendees = dict.fromkeys([self._organizer.id, *self._attendees])  # ordered, deduplicated
        meeting = Meeting(
            id=self._ids.next_id(),
            title=self._title,
            organizer_id=self._organizer.id,
            attendee_ids=tuple(attendees),
            start=self._start,
            duration=self._duration,
            recurrence=self._recurrence,
            room_id=self._room_id,
        )
        meeting.invitations = {
            attendee: Invitation(meeting.id, attendee, InvitationStatus.ACCEPTED)
            if attendee == self._organizer.id
            else Invitation(meeting.id, attendee)
            for attendee in meeting.attendee_ids
        }
        return meeting


# --8<-- [end:builder]
