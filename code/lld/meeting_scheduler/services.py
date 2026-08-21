"""Repositories, availability, room booking, notifications and the scheduler facade."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Protocol

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock, ValidationError
from lld.meeting_scheduler.models import (
    Interval,
    InvitationStatus,
    Meeting,
    MeetingBuilder,
    MeetingNotFoundError,
    MeetingRoom,
    MeetingStatus,
    NoFreeSlotError,
    Reminder,
    RoomBooking,
    SlotConflictError,
    User,
    free_gaps,
    merge_intervals,
)
from lld.meeting_scheduler.recurrence import EarliestSlotFinder, SlotFinder, strategy_for


# --8<-- [start:repository]
class MeetingRepository(Protocol):
    """Repository: the scheduler never touches a dict, so persistence is a swap."""

    def add(self, meeting: Meeting) -> None: ...

    def get(self, meeting_id: str) -> Meeting: ...

    def all(self) -> list[Meeting]: ...

    def for_attendee(self, user_id: str) -> list[Meeting]: ...


class InMemoryMeetingRepository:
    def __init__(self) -> None:
        self._meetings: dict[str, Meeting] = {}
        self._lock = threading.Lock()

    def add(self, meeting: Meeting) -> None:
        with self._lock:
            self._meetings[meeting.id] = meeting

    def get(self, meeting_id: str) -> Meeting:
        with self._lock:
            meeting = self._meetings.get(meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(f"unknown meeting {meeting_id}")
        return meeting

    def all(self) -> list[Meeting]:
        with self._lock:
            return list(self._meetings.values())

    def for_attendee(self, user_id: str) -> list[Meeting]:
        return [m for m in self.all() if user_id in m.attendee_ids]


# --8<-- [end:repository]


# --8<-- [start:availability]
class AvailabilityService:
    """Turns meetings into busy blocks: expand, drop exceptions, merge."""

    def __init__(self, meetings: MeetingRepository) -> None:
        self._meetings = meetings

    def occurrences(self, meeting: Meeting, window: Interval) -> list[Interval]:
        """Every occurrence overlapping ``window``, with cancelled dates removed."""
        # Widen the search so a meeting that starts before the window but runs into it counts.
        expansion = Interval(window.start - meeting.duration, window.end)
        found: list[Interval] = []
        for start in strategy_for(meeting.recurrence).starts(meeting.start, expansion):
            on = start.astimezone(meeting.start.tzinfo).date()
            if on in meeting.cancelled_dates:
                continue
            moved = meeting.moved_dates.get(on)
            actual = moved if moved is not None else start
            found.append(Interval(actual, actual + meeting.duration))
        return found

    def busy(self, user_ids: Sequence[str], window: Interval) -> list[Interval]:
        blocks: list[Interval] = []
        for user_id in user_ids:
            for meeting in self._meetings.for_attendee(user_id):
                if meeting.blocks(user_id):
                    blocks.extend(self.occurrences(meeting, window))
        return merge_intervals(blocks)

    def free(self, user_ids: Sequence[str], window: Interval) -> list[Interval]:
        return free_gaps(self.busy(user_ids, window), window)

    def is_free(self, user_ids: Sequence[str], interval: Interval) -> bool:
        return not any(block.overlaps(interval) for block in self.busy(user_ids, interval))


# --8<-- [end:availability]


# --8<-- [start:rooms]
class RoomRegistry:
    """Rooms and their bookings. One lock per room: the finest granularity that works.

    ``book`` is all-or-nothing across every occurrence of a series - a weekly meeting
    that clashes on one Tuesday books no Tuesdays at all.
    """

    def __init__(self, rooms: Iterable[MeetingRoom]) -> None:
        self._rooms: dict[str, MeetingRoom] = {room.id: room for room in rooms}
        self._bookings: dict[str, list[RoomBooking]] = {room_id: [] for room_id in self._rooms}
        self._locks: dict[str, threading.Lock] = {room_id: threading.Lock() for room_id in self._rooms}

    def room(self, room_id: str) -> MeetingRoom:
        try:
            return self._rooms[room_id]
        except KeyError:
            raise MeetingNotFoundError(f"unknown room {room_id}") from None

    def bookings(self, room_id: str) -> list[RoomBooking]:
        with self._locks[room_id]:
            return list(self._bookings[room_id])

    def book(self, room_id: str, meeting_id: str, intervals: Sequence[Interval]) -> list[RoomBooking]:
        room = self.room(room_id)
        with self._locks[room_id]:
            for existing in self._bookings[room_id]:
                clash = next((i for i in intervals if existing.interval.overlaps(i)), None)
                if clash is not None:
                    raise SlotConflictError(f"{room.name} is already booked for {clash}")
            fresh = [RoomBooking(room_id, meeting_id, interval) for interval in intervals]
            self._bookings[room_id].extend(fresh)
            return fresh

    def release(self, room_id: str, meeting_id: str) -> int:
        with self._locks[room_id]:
            before = len(self._bookings[room_id])
            self._bookings[room_id] = [b for b in self._bookings[room_id] if b.meeting_id != meeting_id]
            return before - len(self._bookings[room_id])

    def book_first_available(
        self, meeting_id: str, intervals: Sequence[Interval], attendees: int
    ) -> str | None:
        """Try rooms smallest-that-fits first. Losing a race is a retry, not an error."""
        candidates = sorted(
            (r for r in self._rooms.values() if r.capacity >= attendees),
            key=lambda r: (r.capacity, r.id),
        )
        for room in candidates:
            try:
                self.book(room.id, meeting_id, intervals)
            except SlotConflictError:
                continue
            return room.id
        return None


# --8<-- [end:rooms]


# --8<-- [start:notifications]
class MeetingObserver(Protocol):
    """Observer: the scheduler announces, it does not know who listens."""

    def on_meeting_event(self, event: str, meeting: Meeting, recipients: Sequence[str]) -> None: ...


class NotificationService:
    def __init__(self) -> None:
        self._inboxes: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def on_meeting_event(self, event: str, meeting: Meeting, recipients: Sequence[str]) -> None:
        message = f"{event}: {meeting.title} ({meeting.first_interval})"
        with self._lock:
            for user_id in recipients:
                self._inboxes.setdefault(user_id, []).append(message)

    def inbox(self, user_id: str) -> list[str]:
        with self._lock:
            return list(self._inboxes.get(user_id, []))

    def total(self) -> int:
        with self._lock:
            return sum(len(messages) for messages in self._inboxes.values())


# --8<-- [end:notifications]


# --8<-- [start:scheduler]
class MeetingScheduler:
    """Facade over repository, availability, rooms and notifications.

    Locks: one per meeting for status and invitation changes (``_lock_for``), one per
    room inside ``RoomRegistry``. Order is always meeting first, then room.
    """

    BOOKING_HORIZON_DAYS = 60

    def __init__(
        self,
        meetings: MeetingRepository,
        rooms: RoomRegistry,
        users: Iterable[User],
        observers: Iterable[MeetingObserver] = (),
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._meetings = meetings
        self._rooms = rooms
        self._users = {user.id: user for user in users}
        self._observers = list(observers)
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("M")
        self._availability = AvailabilityService(meetings)
        self._meeting_locks: dict[str, threading.RLock] = {}
        self._registry_lock = threading.Lock()

    @property
    def availability(self) -> AvailabilityService:
        return self._availability

    @property
    def rooms(self) -> RoomRegistry:
        return self._rooms

    def builder(self) -> MeetingBuilder:
        return MeetingBuilder(self._ids)

    def user(self, user_id: str) -> User:
        try:
            return self._users[user_id]
        except KeyError:
            raise MeetingNotFoundError(f"unknown user {user_id}") from None

    def schedule(self, meeting: Meeting) -> Meeting:
        """Book the room for every occurrence in the horizon, store, then announce."""
        occurrences = self.horizon_occurrences(meeting)
        if meeting.room_id is not None:
            room = self._rooms.room(meeting.room_id)
            if room.capacity < len(meeting.attendee_ids):
                raise ValidationError(
                    f"{room.name} seats {room.capacity}, meeting has {len(meeting.attendee_ids)} attendees"
                )
            self._rooms.book(meeting.room_id, meeting.id, occurrences)
        self._meetings.add(meeting)
        self._announce("invited", meeting)
        return meeting

    def schedule_first_available(
        self,
        title: str,
        organizer: User,
        attendee_ids: Sequence[str],
        minutes: int,
        window: Interval,
        finder: SlotFinder | None = None,
        with_room: bool = True,
    ) -> Meeting:
        """The headline flow: busy blocks, merge, first gap, room, invitations, notify."""
        attendees = tuple(dict.fromkeys([organizer.id, *attendee_ids]))
        slot = self.find_slot(attendees, minutes, window, finder)
        if slot is None:
            raise NoFreeSlotError(f"no {minutes}-minute slot for {len(attendees)} people in {window}")
        meeting = (
            self.builder()
            .titled(title)
            .organized_by(organizer)
            .with_attendees(*attendee_ids)
            .starting_at(slot.start.astimezone(organizer.zone))
            .lasting(minutes)
            .build()
        )
        if with_room:
            room_id = self._rooms.book_first_available(meeting.id, [slot], len(attendees))
            if room_id is None:
                raise NoFreeSlotError(f"no room free for {slot}")
            meeting.room_id = room_id
        self._meetings.add(meeting)
        self._announce("invited", meeting)
        return meeting

    def find_slot(
        self, attendee_ids: Sequence[str], minutes: int, window: Interval, finder: SlotFinder | None = None
    ) -> Interval | None:
        attendees = [self.user(user_id) for user_id in attendee_ids]
        busy = self._availability.busy(attendee_ids, window)
        return (finder or EarliestSlotFinder()).find(attendees, busy, window, timedelta(minutes=minutes))

    def horizon_occurrences(self, meeting: Meeting) -> list[Interval]:
        horizon = Interval(meeting.start, meeting.start + timedelta(days=self.BOOKING_HORIZON_DAYS))
        return self._availability.occurrences(meeting, horizon)

    def respond(self, meeting_id: str, attendee_id: str, status: InvitationStatus) -> None:
        meeting = self._meetings.get(meeting_id)
        with self._lock_for(meeting_id):
            invitation = meeting.invitations.get(attendee_id)
            if invitation is None:
                raise MeetingNotFoundError(f"{attendee_id} was not invited to {meeting_id}")
            invitation.respond(status, self._clock.now())
        self._announce(f"{attendee_id} is {status}", meeting, (meeting.organizer_id,))

    def cancel(self, meeting_id: str) -> Meeting:
        meeting = self._meetings.get(meeting_id)
        with self._lock_for(meeting_id):
            meeting.status = MeetingStatus.CANCELLED
            for invitation in meeting.invitations.values():
                invitation.withdraw()
            if meeting.room_id is not None:
                self._rooms.release(meeting.room_id, meeting.id)
        self._announce("cancelled", meeting)
        return meeting

    def cancel_occurrence(self, meeting_id: str, on: date) -> Meeting:
        """One Tuesday off, the series intact: the exception list, not a new meeting."""
        meeting = self._meetings.get(meeting_id)
        with self._lock_for(meeting_id):
            meeting.cancelled_dates.add(on)
        self._announce(f"occurrence on {on} cancelled", meeting)
        return meeting

    def move_occurrence(self, meeting_id: str, on: date, new_start: datetime) -> Meeting:
        meeting = self._meetings.get(meeting_id)
        with self._lock_for(meeting_id):
            meeting.moved_dates[on] = new_start
        self._announce(f"occurrence on {on} moved", meeting)
        return meeting

    def reschedule(self, meeting_id: str, new_start: datetime) -> RescheduleCommand:
        command = RescheduleCommand(self, self._meetings.get(meeting_id), new_start)
        command.execute()
        return command

    def due_reminders(self, lead_minutes: int = 10) -> list[Reminder]:
        now = self._clock.now_dt()
        window = Interval(now, now + timedelta(minutes=lead_minutes))
        return [
            Reminder(meeting.id, occurrence, lead_minutes)
            for meeting in self._meetings.all()
            if meeting.is_active()
            for occurrence in self._availability.occurrences(meeting, window)
            if now <= occurrence.start < window.end
        ]

    def move(self, meeting: Meeting, new_start: datetime) -> Meeting:
        """Used by the reschedule command in both directions."""
        with self._lock_for(meeting.id):
            if meeting.room_id is not None:
                self._rooms.release(meeting.room_id, meeting.id)
            previous, meeting.start = meeting.start, new_start
            if meeting.room_id is not None:
                try:
                    self._rooms.book(meeting.room_id, meeting.id, self.horizon_occurrences(meeting))
                except SlotConflictError:
                    meeting.start = previous
                    self._rooms.book(meeting.room_id, meeting.id, self.horizon_occurrences(meeting))
                    raise
        self._announce("rescheduled", meeting)
        return meeting

    def _lock_for(self, meeting_id: str) -> threading.RLock:
        with self._registry_lock:
            return self._meeting_locks.setdefault(meeting_id, threading.RLock())

    def _announce(self, event: str, meeting: Meeting, recipients: Sequence[str] | None = None) -> None:
        targets = recipients if recipients is not None else meeting.attendee_ids
        for observer in self._observers:  # outside every lock: a slow listener blocks nobody
            observer.on_meeting_event(event, meeting, targets)


class RescheduleCommand:
    """Command with undo: the organiser moved it by mistake, put it back."""

    def __init__(self, scheduler: MeetingScheduler, meeting: Meeting, new_start: datetime) -> None:
        self._scheduler = scheduler
        self._meeting = meeting
        self._new_start = new_start
        self._previous_start = meeting.start

    def execute(self) -> Meeting:
        return self._scheduler.move(self._meeting, self._new_start)

    def undo(self) -> Meeting:
        return self._scheduler.move(self._meeting, self._previous_start)


# --8<-- [end:scheduler]
