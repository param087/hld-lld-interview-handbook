"""Roles, enrollments, attempts, grades and certificates.

The course *tree* lives in ``content.py``; everything here is the flat data that
hangs off it - who is enrolled, what they answered, and what they earned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from common import (
    ConflictError,
    HandbookError,
    InvalidStateError,
    Money,
    NotFoundError,
    ValidationError,
)

WORDS_PER_MINUTE = 200  # used to turn an article's length into a duration
FREE = Money(0)  # the price of a free course, as a module-level constant


# --8<-- [start:enums]
class Role(StrEnum):
    STUDENT = "student"
    INSTRUCTOR = "instructor"
    ADMIN = "admin"


class PublishStatus(StrEnum):
    DRAFT = "draft"  # only the author and admins can see it
    PUBLISHED = "published"
    ARCHIVED = "archived"  # visible to people already enrolled, closed to new ones


class EnrollmentStatus(StrEnum):
    PENDING = "pending"  # paid course, payment in flight
    ACTIVE = "active"
    WAITLISTED = "waitlisted"  # the course was full when this student arrived
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class LessonKind(StrEnum):
    VIDEO = "video"
    ARTICLE = "article"
    QUIZ = "quiz"
    ASSIGNMENT = "assignment"


class SubmissionStatus(StrEnum):
    SUBMITTED = "submitted"
    GRADED = "graded"


# --8<-- [end:enums]


# --8<-- [start:errors]
class PermissionDeniedError(HandbookError):
    """The viewer's role does not allow this on this course."""


class NotPublishedError(InvalidStateError):
    """The course is still a draft; only its instructor and admins may open it."""


class CourseFullError(ConflictError):
    """Capacity is taken. The student is offered the waiting list instead."""


class PrerequisiteError(ConflictError):
    """A required course has not been completed."""


class PaymentDeclinedError(ConflictError):
    """The card was refused; the seat that was held for the student is released."""


class AlreadyEnrolledError(ConflictError):
    """One live enrollment per student per course."""


class EnrollmentStateError(InvalidStateError):
    """The enrollment is not in a state that allows this operation."""


class AttemptLimitError(ConflictError):
    """The student has used every attempt this quiz allows."""


class AttemptWindowError(InvalidStateError):
    """The attempt was submitted after the quiz's time limit ran out."""


class ContentNotFoundError(NotFoundError):
    """Unknown course, module, lesson or enrollment id."""


# --8<-- [end:errors]


# --8<-- [start:entities]
@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    role: Role = Role.STUDENT


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    prompt: str
    options: tuple[str, ...]
    correct_index: int
    points: int = 1

    def __post_init__(self) -> None:
        if not 0 <= self.correct_index < len(self.options):
            raise ValidationError(f"question {self.id}: correct_index is not one of the options")
        if self.points < 1:
            raise ValidationError(f"question {self.id}: points must be positive")

    def is_correct(self, answer: int | None) -> bool:
        return answer == self.correct_index


@dataclass(frozen=True, slots=True)
class Grade:
    points: int
    max_points: int
    graded_by: str
    graded_at: float
    passed: bool

    @property
    def percent(self) -> float:
        return 0.0 if self.max_points == 0 else round(100 * self.points / self.max_points, 1)

    def __str__(self) -> str:
        return f"{self.points}/{self.max_points} ({self.percent}%) {'pass' if self.passed else 'fail'}"


@dataclass(slots=True)
class QuizAttempt:
    id: str
    quiz_id: str
    student_id: str
    started_at: float
    answers: dict[str, int] = field(default_factory=dict)
    submitted_at: float | None = None
    grade: Grade | None = None


@dataclass(slots=True)
class Submission:
    id: str
    assignment_id: str
    student_id: str
    text: str
    submitted_at: float
    status: SubmissionStatus = SubmissionStatus.SUBMITTED
    grade: Grade | None = None


@dataclass(slots=True)
class Enrollment:
    id: str
    course_id: str
    student_id: str
    status: EnrollmentStatus
    enrolled_at: float
    price: Money = FREE
    completed_lesson_ids: set[str] = field(default_factory=set)
    completed_at: float | None = None

    def is_live(self) -> bool:
        return self.status in (
            EnrollmentStatus.PENDING,
            EnrollmentStatus.ACTIVE,
            EnrollmentStatus.WAITLISTED,
            EnrollmentStatus.COMPLETED,
        )

    def takes_a_seat(self) -> bool:
        """Waitlisted students hold no seat; everyone else who is live does."""
        return self.status in (
            EnrollmentStatus.PENDING,
            EnrollmentStatus.ACTIVE,
            EnrollmentStatus.COMPLETED,
        )

    def activate(self) -> None:
        if self.status not in (EnrollmentStatus.PENDING, EnrollmentStatus.WAITLISTED):
            raise EnrollmentStateError(f"enrollment {self.id} is {self.status}, cannot activate")
        self.status = EnrollmentStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class Progress:
    """A snapshot, computed against the course tree as it exists right now."""

    completed: int
    total: int
    minutes_done: int
    minutes_total: int

    @property
    def percent(self) -> float:
        return 0.0 if self.total == 0 else round(100 * self.completed / self.total, 1)

    def is_finished(self) -> bool:
        return self.total > 0 and self.completed == self.total

    def __str__(self) -> str:
        return f"{self.completed}/{self.total} lessons ({self.percent}%), {self.minutes_done}/{self.minutes_total} min"


@dataclass(frozen=True, slots=True)
class Certificate:
    id: str
    course_id: str
    student_id: str
    issued_at: float
    score_percent: float


@dataclass(frozen=True, slots=True)
class DiscussionPost:
    id: str
    lesson_id: str
    author_id: str
    body: str
    posted_at: float


# --8<-- [end:entities]
