"""A daily standup across a DST switch, a free-slot search, and a room clash."""

from datetime import UTC, date, datetime, timedelta

from common import FakeClock, HandbookError, SequentialIdGenerator
from lld.meeting_scheduler.models import (
    Frequency,
    Interval,
    InvitationStatus,
    MeetingRoom,
    RecurrenceRule,
    User,
)
from lld.meeting_scheduler.recurrence import WorkingHoursSlotFinder
from lld.meeting_scheduler.services import (
    InMemoryMeetingRepository,
    MeetingScheduler,
    NotificationService,
    RoomRegistry,
)

ADA = User("ada", "Ada", "Europe/Berlin")
LINUS = User("linus", "Linus", "America/New_York")
GRACE = User("grace", "Grace", "Asia/Kolkata")


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def main() -> None:
    clock = FakeClock(start=utc("2026-03-26 06:00").timestamp())
    repository = InMemoryMeetingRepository()
    rooms = RoomRegistry([MeetingRoom("R-1", "Turing", 6), MeetingRoom("R-2", "Hopper", 12)])
    inboxes = NotificationService()
    scheduler = MeetingScheduler(
        repository, rooms, [ADA, LINUS, GRACE], [inboxes], clock=clock, ids=SequentialIdGenerator("M")
    )

    standup = scheduler.schedule(
        scheduler.builder()
        .titled("Standup")
        .organized_by(ADA)
        .with_attendees(LINUS.id, GRACE.id)
        .starting_at("2026-03-25 09:00")
        .lasting(15)
        .repeating(RecurrenceRule(Frequency.DAILY))
        .in_room("R-1")
        .build()
    )
    week = Interval(utc("2026-03-25 00:00"), utc("2026-04-01 00:00"))
    occurrences = scheduler.availability.occurrences(standup, week)
    print(f"{standup.id} {standup.title}: 09:00 Europe/Berlin daily, {len(occurrences)} in the week")
    print(f"before the DST switch: {occurrences[0]} | after it: {occurrences[-1]}")

    scheduler.cancel_occurrence(standup.id, date(2026, 3, 27))
    print(f"cancelled 2026-03-27: {len(scheduler.availability.occurrences(standup, week))} occurrences left")

    scheduler.schedule(
        scheduler.builder()
        .titled("Design review")
        .organized_by(ADA)
        .with_attendees(LINUS.id)
        .starting_at("2026-03-26 14:00")
        .lasting(60)
        .in_room("R-1")
        .build()
    )
    day = Interval(utc("2026-03-26 08:00"), utc("2026-03-26 18:00"))
    print(f"ada is busy {[str(b) for b in scheduler.availability.busy([ADA.id], day)]}")
    print(f"first 30 min for ada+linus: {scheduler.find_slot([ADA.id, LINUS.id], 30, day)}")
    working = scheduler.find_slot([ADA.id, LINUS.id], 30, day, WorkingHoursSlotFinder(9, 17))
    print(f"inside 09:00-17:00 in Berlin and New York: {working}")
    print(f"adding Grace in Kolkata: {scheduler.find_slot([ADA.id, LINUS.id, GRACE.id], 30, day, WorkingHoursSlotFinder(9, 17))}")

    try:
        scheduler.schedule(
            scheduler.builder()
            .titled("Retro")
            .organized_by(ADA)
            .starting_at("2026-03-26 14:30")
            .lasting(30)
            .in_room("R-1")
            .build()
        )
    except HandbookError as exc:
        print(f"room clash: {exc}")

    scheduler.respond(standup.id, LINUS.id, InvitationStatus.DECLINED)
    scheduler.respond(standup.id, GRACE.id, InvitationStatus.TENTATIVE)
    statuses = {uid: inv.status.value for uid, inv in standup.invitations.items()}
    print(f"invitations: {statuses}")

    command = scheduler.reschedule(standup.id, standup.start + timedelta(hours=2))
    print(f"rescheduled standup to {standup.first_interval}")
    command.undo()
    print(f"undo put it back to {standup.first_interval}; {inboxes.total()} notifications sent")
    clock.set(utc("2026-03-26 07:55").timestamp())
    print(f"reminders due at 07:55Z: {[r.meeting_id for r in scheduler.due_reminders(10)]}")


if __name__ == "__main__":
    main()
