"""When a task runs next: four Strategy implementations behind one Protocol.

Each answers exactly two questions - when is the first run, and given the run
that just finished, when is the next one - and ``None`` means "never again".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from lld.task_scheduler.models import ExecutionRecord, OverrunPolicy, ScheduleError

CRON_FIELDS = ("minute", "hour", "day", "month", "weekday")
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
MAX_CRON_STEPS = 5_000  # a whole day of minutes plus four years of day skips


# --8<-- [start:protocol]
class Schedule(Protocol):
    """A recurrence rule. Pure and stateless, so one instance is safely shared."""

    def first_run(self, now: float) -> float | None:
        """When this task should run for the first time."""

    def next_run(self, record: ExecutionRecord, now: float) -> float | None:
        """When to run again after ``record``. ``None`` retires the task."""


@dataclass(frozen=True, slots=True)
class OneTime:
    """Run once, at ``at`` - or immediately if that instant has already passed."""

    at: float

    def first_run(self, now: float) -> float | None:
        return max(self.at, now)

    def next_run(self, record: ExecutionRecord, now: float) -> float | None:
        return None


@dataclass(frozen=True, slots=True)
class FixedDelay:
    """Wait ``delay`` seconds *after the previous run finished*.

    Two runs can therefore never overlap, whatever a run costs. This is the right
    default for jobs whose duration varies - a backup, a sync, an import.
    """

    delay: float
    start_delay: float = 0.0

    def first_run(self, now: float) -> float | None:
        return now + self.start_delay

    def next_run(self, record: ExecutionRecord, now: float) -> float | None:
        return record.finished_at + self.delay


@dataclass(frozen=True, slots=True)
class FixedRate:
    """Fire every ``period`` seconds measured from when a run *started*.

    The interesting case is a run that outlasts its own period. ``SKIP`` jumps to
    the next slot in the future, so a job that hangs for an hour does not come
    back to a queue of sixty missed minutes. ``CATCH_UP`` runs the backlog. Pick
    one deliberately: the default of "just keep adding" is how schedulers melt.
    """

    period: float
    on_overrun: OverrunPolicy = OverrunPolicy.SKIP
    start_delay: float = 0.0

    def first_run(self, now: float) -> float | None:
        return now + self.start_delay

    def next_run(self, record: ExecutionRecord, now: float) -> float | None:
        due = record.started_at + self.period
        if due > now or self.on_overrun is OverrunPolicy.CATCH_UP:
            return due
        missed = int((now - due) // self.period) + 1
        return due + missed * self.period


# --8<-- [end:protocol]


# --8<-- [start:cron]
@dataclass(frozen=True, slots=True)
class CronSchedule:
    """A five-field cron expression evaluated in UTC, to the minute.

    ``*/15 * * * *`` is every quarter hour, ``0 3 * * 1-5`` is 03:00 on weekdays.
    The search steps minute by minute but skips a whole day as soon as the date
    fields do not match, so finding the next fire time never walks a year of
    minutes. When both day-of-month and day-of-week are restricted, cron's
    historical rule applies: either one matching is enough.
    """

    expression: str

    def __post_init__(self) -> None:
        self._parsed()  # fail at construction, not on the first tick

    def first_run(self, now: float) -> float | None:
        return self.next_after(now)

    def next_run(self, record: ExecutionRecord, now: float) -> float | None:
        return self.next_after(max(now, record.finished_at))

    def next_after(self, timestamp: float) -> float:
        minutes, hours, days, months, weekdays = self._parsed()
        moment = datetime.fromtimestamp(timestamp, UTC).replace(second=0, microsecond=0)
        moment += timedelta(minutes=1)
        for _ in range(MAX_CRON_STEPS):
            if not _date_matches(moment, days, months, weekdays):
                moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if moment.hour in hours and moment.minute in minutes:
                return moment.timestamp()
            moment += timedelta(minutes=1)
        raise ScheduleError(f"cron expression {self.expression!r} has no run time in range")

    def _parsed(self) -> tuple[frozenset[int], ...]:
        parts = self.expression.split()
        if len(parts) != len(CRON_FIELDS):
            raise ScheduleError(f"cron needs {len(CRON_FIELDS)} fields, got {len(parts)}: {self.expression!r}")
        return tuple(
            _parse_field(part, low, high, name)
            for part, (low, high), name in zip(parts, CRON_RANGES, CRON_FIELDS, strict=True)
        )


def _date_matches(
    moment: datetime, days: frozenset[int], months: frozenset[int], weekdays: frozenset[int]
) -> bool:
    if moment.month not in months:
        return False
    # cron weekdays are Sunday=0; Python's weekday() is Monday=0
    day_ok = moment.day in days
    weekday_ok = (moment.weekday() + 1) % 7 in weekdays
    restricted_day = len(days) < 31
    restricted_weekday = len(weekdays) < 7
    if restricted_day and restricted_weekday:
        return day_ok or weekday_ok
    return day_ok and weekday_ok


def _parse_field(spec: str, low: int, high: int, name: str) -> frozenset[int]:
    """``*``, ``5``, ``1-5``, ``*/15`` and comma-separated lists of those."""
    values: set[int] = set()
    for part in spec.split(","):
        body, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
        except ValueError as exc:
            raise ScheduleError(f"cron {name} field has a bad step: {part!r}") from exc
        if step < 1:
            raise ScheduleError(f"cron {name} field step must be positive: {part!r}")
        start, end = _bounds(body, low, high, name)
        values.update(range(start, end + 1, step))
    if not values:
        raise ScheduleError(f"cron {name} field matches nothing: {spec!r}")
    return frozenset(values)


def _bounds(body: str, low: int, high: int, name: str) -> tuple[int, int]:
    if body in ("*", ""):
        return low, high
    first, sep, last = body.partition("-")
    try:
        start = int(first)
        end = int(last) if sep else start
    except ValueError as exc:
        raise ScheduleError(f"cron {name} field is not a number: {body!r}") from exc
    if not (low <= start <= high and low <= end <= high and start <= end):
        raise ScheduleError(f"cron {name} field {body!r} is outside {low}-{high}")
    return start, end


# --8<-- [end:cron]
