from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common import FakeClock, SequentialIdGenerator, ValidationError
from lld.meeting_scheduler.models import (
    Frequency,
    Interval,
    InvitationStateError,
    InvitationStatus,
    Meeting,
    MeetingRoom,
    RecurrenceRule,
    SlotConflictError,
    User,
    free_gaps,
    merge_intervals,
)
from lld.meeting_scheduler.recurrence import WorkingHoursSlotFinder, localise
from lld.meeting_scheduler.services import (
    InMemoryMeetingRepository,
    MeetingScheduler,
    NotificationService,
    RoomRegistry,
)

ADA = User("ada", "Ada", "Europe/Berlin")
LINUS = User("linus", "Linus", "America/New_York")
GRACE = User("grace", "Grace", "Asia/Kolkata")
BERLIN = ZoneInfo("Europe/Berlin")


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def interval(start: str, end: str) -> Interval:
    return Interval(utc(start), utc(end))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=utc("2026-03-26 06:00").timestamp())


@pytest.fixture
def scheduler(clock: FakeClock) -> MeetingScheduler:
    rooms = RoomRegistry([MeetingRoom("R-1", "Turing", 4), MeetingRoom("R-2", "Hopper", 10)])
    return MeetingScheduler(
        InMemoryMeetingRepository(),
        rooms,
        [ADA, LINUS, GRACE],
        [NotificationService()],
        clock=clock,
        ids=SequentialIdGenerator("M"),
    )


def book(
    scheduler: MeetingScheduler, title: str, start: str, minutes: int, *guests: str, room: str | None = None
) -> Meeting:
    return scheduler.schedule(
        scheduler.builder()
        .titled(title)
        .organized_by(ADA)
        .with_attendees(*guests)
        .starting_at(start)
        .lasting(minutes)
        .in_room(room)
        .build()
    )


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (("10:00", "11:00"), ("11:00", "12:00"), False),  # back to back is not a clash
        (("10:00", "11:00"), ("10:59", "12:00"), True),
        (("10:00", "12:00"), ("10:30", "11:00"), True),
        (("10:00", "11:00"), ("09:00", "10:00"), False),
    ],
)
def test_intervals_are_half_open(first: tuple[str, str], second: tuple[str, str], expected: bool) -> None:
    left = interval(f"2026-03-26 {first[0]}", f"2026-03-26 {first[1]}")
    right = interval(f"2026-03-26 {second[0]}", f"2026-03-26 {second[1]}")
    assert left.overlaps(right) is expected and right.overlaps(left) is expected


def test_merge_and_gaps_are_inverses() -> None:
    busy = [
        interval("2026-03-26 10:00", "2026-03-26 11:00"),
        interval("2026-03-26 10:30", "2026-03-26 12:00"),
        interval("2026-03-26 14:00", "2026-03-26 15:00"),
    ]
    window = interval("2026-03-26 09:00", "2026-03-26 16:00")
    assert [str(b) for b in merge_intervals(busy)] == [
        "2026-03-26 10:00-12:00Z",
        "2026-03-26 14:00-15:00Z",
    ]
    assert [str(g) for g in free_gaps(busy, window)] == [
        "2026-03-26 09:00-10:00Z",
        "2026-03-26 12:00-14:00Z",
        "2026-03-26 15:00-16:00Z",
    ]


# --8<-- [start:dst]
def test_a_daily_meeting_keeps_local_time_across_the_dst_switch(scheduler: MeetingScheduler) -> None:
    standup = scheduler.schedule(
        scheduler.builder()
        .titled("Standup")
        .organized_by(ADA)
        .starting_at("2026-03-25 09:00")  # 09:00 in Europe/Berlin
        .lasting(15)
        .repeating(RecurrenceRule(Frequency.DAILY))
        .build()
    )
    week = interval("2026-03-25 00:00", "2026-04-01 00:00")
    starts = [o.start.astimezone(UTC).strftime("%m-%d %H:%M") for o in scheduler.availability.occurrences(standup, week)]
    assert starts[:2] == ["03-25 08:00", "03-26 08:00"]  # CET, UTC+1
    assert starts[-2:] == ["03-30 07:00", "03-31 07:00"]  # CEST, UTC+2 - same 09:00 local
    assert len(starts) == 7


def test_a_wall_clock_time_that_does_not_exist_moves_forward() -> None:
    gap = localise(datetime(2026, 3, 29, 2, 30), BERLIN)  # the clocks jump 02:00 -> 03:00
    assert gap.hour == 3 and gap.minute == 30
    normal = localise(datetime(2026, 3, 30, 2, 30), BERLIN)
    assert normal.hour == 2 and normal.minute == 30


# --8<-- [end:dst]


def test_weekly_recurrence_expands_only_the_named_weekdays(scheduler: MeetingScheduler) -> None:
    meeting = scheduler.schedule(
        scheduler.builder()
        .titled("Pairing")
        .organized_by(ADA)
        .starting_at("2026-03-23 10:00")  # a Monday
        .lasting(30)
        .repeating(RecurrenceRule(Frequency.WEEKLY, weekdays=(0, 2)))
        .build()
    )
    window = interval("2026-03-23 00:00", "2026-04-06 00:00")
    days = [o.start.astimezone(BERLIN).strftime("%a %m-%d") for o in scheduler.availability.occurrences(meeting, window)]
    assert days == ["Mon 03-23", "Wed 03-25", "Mon 03-30", "Wed 04-01"]


def test_cancelling_one_occurrence_leaves_the_series_alone(scheduler: MeetingScheduler) -> None:
    standup = scheduler.schedule(
        scheduler.builder()
        .titled("Standup")
        .organized_by(ADA)
        .starting_at("2026-03-25 09:00")
        .lasting(15)
        .repeating(RecurrenceRule(Frequency.DAILY, count=5))
        .build()
    )
    week = interval("2026-03-25 00:00", "2026-04-01 00:00")
    scheduler.cancel_occurrence(standup.id, date(2026, 3, 27))
    starts = [o.start.astimezone(BERLIN).date() for o in scheduler.availability.occurrences(standup, week)]
    assert date(2026, 3, 27) not in starts and len(starts) == 4
    assert standup.is_active()


def test_first_free_slot_skips_every_attendee_busy_block(scheduler: MeetingScheduler) -> None:
    book(scheduler, "1:1", "2026-03-26 09:00", 60, LINUS.id)  # 08:00-09:00Z
    book(scheduler, "Review", "2026-03-26 10:30", 30, GRACE.id)  # 09:30-10:00Z
    day = interval("2026-03-26 08:00", "2026-03-26 18:00")
    slot = scheduler.find_slot([ADA.id, LINUS.id, GRACE.id], 30, day)
    assert str(slot) == "2026-03-26 09:00-09:30Z"


def test_working_hours_across_three_continents_have_no_common_slot(scheduler: MeetingScheduler) -> None:
    day = interval("2026-03-26 00:00", "2026-03-27 00:00")
    two = scheduler.find_slot([ADA.id, LINUS.id], 30, day, WorkingHoursSlotFinder(9, 17))
    three = scheduler.find_slot([ADA.id, LINUS.id, GRACE.id], 30, day, WorkingHoursSlotFinder(9, 17))
    assert str(two) == "2026-03-26 13:00-13:30Z"  # Berlin afternoon, New York morning
    assert three is None


def test_a_declined_invitation_frees_the_attendee(scheduler: MeetingScheduler) -> None:
    meeting = book(scheduler, "Optional sync", "2026-03-26 15:00", 60, LINUS.id)
    day = interval("2026-03-26 08:00", "2026-03-26 18:00")
    assert scheduler.availability.busy([LINUS.id], day) != []
    scheduler.respond(meeting.id, LINUS.id, InvitationStatus.DECLINED)
    assert scheduler.availability.busy([LINUS.id], day) == []
    assert scheduler.availability.busy([ADA.id], day) != []  # the organiser is still busy


def test_a_cancelled_meeting_withdraws_its_invitations(scheduler: MeetingScheduler) -> None:
    meeting = book(scheduler, "Standup", "2026-03-26 09:00", 15, LINUS.id, room="R-1")
    scheduler.cancel(meeting.id)
    assert meeting.invitations[LINUS.id].status is InvitationStatus.WITHDRAWN
    assert scheduler.rooms.bookings("R-1") == []  # cancelling gives the room back
    with pytest.raises(InvitationStateError):
        scheduler.respond(meeting.id, LINUS.id, InvitationStatus.ACCEPTED)


def test_builder_refuses_an_incomplete_meeting() -> None:
    builder = MeetingScheduler(
        InMemoryMeetingRepository(), RoomRegistry([]), [ADA], ids=SequentialIdGenerator("M")
    ).builder()
    with pytest.raises(ValidationError, match="title"):
        builder.build()
    with pytest.raises(ValidationError, match="organiser"):
        builder.titled("Sync").build()
    with pytest.raises(ValidationError, match="start time"):
        builder.organized_by(ADA).build()


# --8<-- [start:reschedule]
def test_reschedule_moves_the_room_booking_and_undo_puts_it_back(scheduler: MeetingScheduler) -> None:
    meeting = book(scheduler, "Design review", "2026-03-26 14:00", 60, LINUS.id, room="R-1")
    command = scheduler.reschedule(meeting.id, meeting.start + timedelta(hours=2))
    booked = [str(b.interval) for b in scheduler.rooms.bookings("R-1")]
    assert booked == ["2026-03-26 15:00-16:00Z"]

    command.undo()
    assert [str(b.interval) for b in scheduler.rooms.bookings("R-1")] == ["2026-03-26 13:00-14:00Z"]


# --8<-- [end:reschedule]


# --8<-- [start:concurrency]
def test_only_one_of_many_racing_bookings_gets_the_room(scheduler: MeetingScheduler) -> None:
    slot = interval("2026-03-26 13:00", "2026-03-26 14:00")

    def grab(i: int) -> str | None:
        try:
            scheduler.rooms.book("R-1", f"M-{i}", [slot])
        except SlotConflictError:
            return None
        return f"M-{i}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [name for name in pool.map(grab, range(8)) if name is not None]

    assert len(winners) == 1  # the per-room lock decides, everybody else is told no
    assert len(scheduler.rooms.bookings("R-1")) == 1


# --8<-- [end:concurrency]
