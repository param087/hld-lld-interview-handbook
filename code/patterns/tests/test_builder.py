"""Builder: stepwise construction, all-at-once validation, the keyword-only and replace() forms."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from common import ValidationError
from patterns.builder import (
    Meeting,
    MeetingBuilder,
    Recurrence,
    daily_standup,
    reschedule,
    schedule,
)

NINE = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def test_fluent_steps_build_a_complete_meeting() -> None:
    meeting = (
        MeetingBuilder("Design review", "ana")
        .starting_at(NINE)
        .lasting(45)
        .invite("ben", "chen")
        .invite("ben")  # duplicates collapse: attendees are a set
        .in_room("Board room")
        .repeating(Recurrence.WEEKLY)
        .build()
    )
    assert meeting == Meeting(
        "Design review",
        "ana",
        NINE,
        NINE + timedelta(minutes=45),
        frozenset({"ana", "ben", "chen"}),
        "Board room",
        Recurrence.WEEKLY,
    )
    assert meeting.duration == timedelta(minutes=45)


def test_builder_can_be_completed_later_by_someone_else() -> None:
    draft = MeetingBuilder("1:1", "ana").invite("ben").lasting(30)  # the form knows this much
    with pytest.raises(ValidationError, match="start time is required"):
        draft.build()  # incomplete is legal for the builder, illegal for the product
    meeting = draft.starting_at(NINE + timedelta(hours=2)).build()  # the slot finder knows the rest
    assert meeting.start == NINE + timedelta(hours=2)
    assert meeting.end == NINE + timedelta(hours=2, minutes=30)


def test_build_reports_every_problem_at_once() -> None:
    with pytest.raises(ValidationError) as info:
        MeetingBuilder("  ", "ana").starting_at(NINE).lasting(9 * 60).build()
    message = str(info.value)
    assert "title is required" in message and "cannot last more than" in message


@pytest.mark.parametrize(
    ("builder", "problem"),
    [
        (MeetingBuilder("Sync", "ana").starting_at(NINE), "end time or a duration"),
        (MeetingBuilder("Sync", "ana").starting_at(NINE).lasting(0), "end must be after start"),
        (
            MeetingBuilder("Sync", "ana").starting_at(NINE).ending_at(NINE - timedelta(minutes=5)),
            "end must be after start",
        ),
        (
            MeetingBuilder("Sync", "ana").starting_at(NINE.replace(tzinfo=None)).lasting(30),
            "timezone-aware",
        ),
        (MeetingBuilder("Sync", "").starting_at(NINE).lasting(30), "organizer is required"),
    ],
)
def test_each_invariant_is_named_in_the_error(builder: MeetingBuilder, problem: str) -> None:
    with pytest.raises(ValidationError, match=problem):
        builder.build()


def test_the_last_of_ending_at_and_lasting_wins() -> None:
    by_duration = MeetingBuilder("Sync", "ana").starting_at(NINE).ending_at(NINE + timedelta(hours=2))
    assert by_duration.lasting(30).build().duration == timedelta(minutes=30)
    by_end = MeetingBuilder("Sync", "ana").starting_at(NINE).lasting(30)
    assert by_end.ending_at(NINE + timedelta(hours=2)).build().duration == timedelta(hours=2)


def test_builder_adds_the_organizer_but_the_product_still_requires_it() -> None:
    assert "ana" in MeetingBuilder("Sync", "ana").starting_at(NINE).lasting(30).build().attendees
    with pytest.raises(ValidationError, match="organizer must attend"):
        Meeting("Sync", "ana", NINE, NINE + timedelta(minutes=30), frozenset({"ben"}))


def test_product_is_immutable_and_replace_revalidates() -> None:
    meeting = schedule(title="Sync", organizer="ana", start=NINE, minutes=30, attendees=["ben"])
    with pytest.raises(FrozenInstanceError):
        meeting.room = "B2"  # type: ignore[misc]
    moved = meeting.moved_to("B2")
    assert moved.room == "B2" and meeting.room is None
    assert len({meeting, moved}) == 2  # hashable: frozen values work as set members and dict keys
    with pytest.raises(ValidationError, match="end must be after start"):
        replace(meeting, end=meeting.start)


def test_keyword_only_schedule_matches_the_builder_and_reschedule_keeps_the_duration() -> None:
    built = MeetingBuilder("Sync", "ana").starting_at(NINE).lasting(30).invite("ben").build()
    assert schedule(title="Sync", organizer="ana", start=NINE, minutes=30, attendees=["ben"]) == built
    with pytest.raises(TypeError):
        schedule("Sync", "ana", NINE, 30)  # type: ignore[misc]  # positional use is a TypeError
    moved = reschedule(built, NINE + timedelta(days=1))
    assert moved.duration == built.duration and moved.start == NINE + timedelta(days=1)
    assert built.start == NINE  # the original is untouched


def test_director_applies_the_recipe() -> None:
    standup = daily_standup("ana", ["ben", "chen"], NINE, room="Huddle")
    assert standup.title == "Daily stand-up"
    assert standup.recurrence is Recurrence.DAILY
    assert standup.duration == timedelta(minutes=15)
    assert standup.attendees == frozenset({"ana", "ben", "chen"})
    assert daily_standup("ana", [], NINE).room is None
