"""Builder: construct an object step by step and validate it once.

The running example is a calendar ``Meeting``: a frozen value with two required
parts and several optional ones that arrive from different places (the form, the
slot finder, the room service). ``MeetingBuilder`` is the object that is allowed
to be incomplete; ``build`` derives what it can, checks every invariant and
reports all problems at once, and the ``Meeting`` it returns cannot be invalid.
The second section shows the Pythonic forms: keyword-only arguments with
defaults, and ``dataclasses.replace`` on a frozen value.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self

from common import ValidationError

MAX_DURATION_HOURS = 8
MAX_DURATION = timedelta(hours=MAX_DURATION_HOURS)
STANDUP_MINUTES = 15


# --8<-- [start:product]
class Recurrence(StrEnum):
    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"


def meeting_problems(
    title: str,
    organizer: str,
    start: datetime | None,
    end: datetime | None,
    attendees: frozenset[str],
) -> list[str]:
    """Every invariant in one place, shared by the product and the builder.

    Returns *all* violations rather than raising on the first, so a caller can fix
    a form in one round trip instead of five.
    """
    problems: list[str] = []
    if not title.strip():
        problems.append("title is required")
    if not organizer.strip():
        problems.append("organizer is required")
    if start is None:
        problems.append("a start time is required")
    if end is None:
        problems.append("an end time or a duration is required")
    if start is not None and end is not None:
        if start.tzinfo is None or end.tzinfo is None:
            problems.append("start and end must be timezone-aware")
        elif end <= start:
            problems.append("end must be after start")
        elif end - start > MAX_DURATION:
            problems.append(f"a meeting cannot last more than {MAX_DURATION_HOURS} hours")
    if organizer not in attendees:
        problems.append("the organizer must attend")
    return problems


@dataclass(frozen=True, slots=True)
class Meeting:
    """The product: immutable and never invalid, because ``__post_init__`` enforces the invariants.

    Every way in, the builder, keyword arguments or ``replace``, ends here.
    """

    title: str
    organizer: str
    start: datetime
    end: datetime
    attendees: frozenset[str] = frozenset()
    room: str | None = None
    recurrence: Recurrence = Recurrence.NONE

    def __post_init__(self) -> None:
        problems = meeting_problems(
            self.title, self.organizer, self.start, self.end, self.attendees
        )
        if problems:
            raise ValidationError("; ".join(problems))

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def moved_to(self, room: str) -> Meeting:
        """A with-er: ``replace`` copies, changes one field and re-runs the validation."""
        return replace(self, room=room)


# --8<-- [end:product]


# --8<-- [start:builder]
class MeetingBuilder:
    """The object that is allowed to be incomplete.

    Required parts go in the constructor, so a builder never lacks what it could
    have been given up front. Optional parts arrive through steps that return
    ``self``, from whichever part of the program knows them. ``build`` derives the
    end from a duration, adds the organizer to the attendees, validates everything
    at once and returns a ``Meeting``. A builder is single-use and single-threaded:
    one per construction, then discarded, so it needs no lock.
    """

    def __init__(self, title: str, organizer: str) -> None:
        self._title = title
        self._organizer = organizer
        self._start: datetime | None = None
        self._end: datetime | None = None
        self._duration: timedelta | None = None
        self._attendees: set[str] = set()
        self._room: str | None = None
        self._recurrence = Recurrence.NONE

    def starting_at(self, start: datetime) -> Self:
        self._start = start
        return self

    def ending_at(self, end: datetime) -> Self:
        self._end, self._duration = end, None
        return self

    def lasting(self, minutes: int) -> Self:
        self._duration, self._end = timedelta(minutes=minutes), None
        return self

    def invite(self, *attendees: str) -> Self:
        self._attendees.update(attendees)
        return self

    def in_room(self, room: str) -> Self:
        self._room = room
        return self

    def repeating(self, recurrence: Recurrence) -> Self:
        self._recurrence = recurrence
        return self

    def build(self) -> Meeting:
        start, end = self._start, self._end
        if start is not None and end is None and self._duration is not None:
            end = start + self._duration
        attendees = frozenset(self._attendees | {self._organizer})
        problems = meeting_problems(self._title, self._organizer, start, end, attendees)
        if problems:
            raise ValidationError("; ".join(problems))
        assert start is not None and end is not None  # proven by the check above
        return Meeting(
            self._title, self._organizer, start, end, attendees, self._room, self._recurrence
        )


def daily_standup(
    organizer: str, team: Iterable[str], first_day: datetime, room: str | None = None
) -> Meeting:
    """The Director: a recipe that drives the builder. A recipe is a function, so it is one."""
    builder = (
        MeetingBuilder("Daily stand-up", organizer)
        .starting_at(first_day)
        .lasting(STANDUP_MINUTES)
        .invite(*team)
        .repeating(Recurrence.DAILY)
    )
    if room is not None:
        builder.in_room(room)
    return builder.build()


# --8<-- [end:builder]


# --8<-- [start:pythonic]
def schedule(
    *,
    title: str,
    organizer: str,
    start: datetime,
    minutes: int,
    attendees: Iterable[str] = (),
    room: str | None = None,
) -> Meeting:
    """Keyword-only arguments with defaults.

    When every part is known at one call site, the signature is the builder: names
    instead of steps, defaults instead of optional setters, and a ``TypeError`` for
    a positional call or a missing name.
    """
    end = start + timedelta(minutes=minutes)
    return Meeting(title, organizer, start, end, frozenset(attendees) | {organizer}, room)


def reschedule(meeting: Meeting, start: datetime) -> Meeting:
    """``dataclasses.replace`` is the builder for frozen values: copy, change, re-validate.

    ``__post_init__`` runs again on the copy, so a change that breaks an invariant
    fails exactly the way a bad ``build`` does, and the original is untouched.
    """
    return replace(meeting, start=start, end=start + meeting.duration)


# --8<-- [end:pythonic]


def main() -> None:
    nine = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    print("--- fluent steps, then one validation ---")
    review = (
        MeetingBuilder("Design review", "ana")
        .starting_at(nine)
        .lasting(45)
        .invite("ben", "chen")
        .in_room("Board room")
        .build()
    )
    print(
        f"{review.title}: {review.start:%H:%M}-{review.end:%H:%M} in {review.room}, "
        f"{sorted(review.attendees)}"
    )

    print("--- the builder travels half-built: the slot finder supplies the start later ---")
    draft = MeetingBuilder("1:1", "ana").invite("ben").lasting(30)
    free_slot = nine + timedelta(hours=2)  # what an availability service would return
    one_on_one = draft.starting_at(free_slot).build()
    print(f"{one_on_one.title}: {one_on_one.start:%H:%M}-{one_on_one.end:%H:%M}")

    print("--- every problem at once, not just the first ---")
    try:
        MeetingBuilder("  ", "ana").starting_at(nine).lasting(9 * 60).build()
    except ValidationError as exc:
        print(f"rejected: {exc}")

    print("--- the director: a recipe is a function ---")
    standup = daily_standup("ana", ["ben", "chen"], nine, room="Huddle")
    minutes = standup.duration.seconds // 60
    print(
        f"{standup.title}: {standup.recurrence.value}, {minutes} min, "
        f"{len(standup.attendees)} attendees"
    )

    print("--- Pythonic: keyword-only arguments, then replace() for changes ---")
    sync = schedule(title="Weekly sync", organizer="ana", start=nine, minutes=30, attendees=["ben"])
    moved = reschedule(sync, nine + timedelta(days=1))
    print(
        f"moved from {sync.start:%a %H:%M} to {moved.start:%a %H:%M}; "
        f"original still {sync.start:%a %H:%M}"
    )
    try:
        Meeting("Bad", "ana", nine, nine, frozenset({"ana"}))  # cannot skip the invariants
    except ValidationError as exc:
        print(f"rejected: {exc}")


if __name__ == "__main__":
    main()
