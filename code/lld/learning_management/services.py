"""Catalogue, permissions, the access proxy, notifications and enrollment."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Protocol

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock
from lld.learning_management.content import ContentNode, Course, Lesson
from lld.learning_management.models import (
    AlreadyEnrolledError,
    ContentNotFoundError,
    CourseFullError,
    Enrollment,
    EnrollmentStatus,
    NotPublishedError,
    PaymentDeclinedError,
    PermissionDeniedError,
    PrerequisiteError,
    PublishStatus,
    Role,
    User,
)
from lld.learning_management.visitors import ContentVisitor


# --8<-- [start:catalog]
class CourseRepository(Protocol):
    """Repository: the services never touch a dict, so persistence is a swap."""

    def add(self, course: Course) -> Course: ...

    def get(self, course_id: str) -> Course: ...

    def all(self) -> list[Course]: ...


class CourseCatalog:
    """Stores courses and owns the publish/archive transitions. No permission logic."""

    def __init__(self) -> None:
        self._courses: dict[str, Course] = {}
        self._lock = threading.Lock()

    def add(self, course: Course) -> Course:
        with self._lock:
            self._courses[course.id] = course
        return course

    def get(self, course_id: str) -> Course:
        with self._lock:
            course = self._courses.get(course_id)
        if course is None:
            raise ContentNotFoundError(f"unknown course {course_id}")
        return course

    def all(self) -> list[Course]:
        with self._lock:
            return list(self._courses.values())

    def published(self) -> list[Course]:
        return [course for course in self.all() if course.status is PublishStatus.PUBLISHED]

    def set_status(self, course_id: str, status: PublishStatus) -> Course:
        course = self.get(course_id)
        with self._lock:
            course.status = status
        return course


# --8<-- [end:catalog]


# --8<-- [start:permissions]
class EnrollmentLookup(Protocol):
    def is_enrolled(self, course_id: str, student_id: str) -> bool: ...


class PermissionService:
    """One place that answers "may this person see or change this course?"."""

    def __init__(self, enrollments: EnrollmentLookup | None = None) -> None:
        self._enrollments = enrollments

    def can_edit(self, viewer: User, course: Course) -> bool:
        return viewer.role is Role.ADMIN or viewer.id == course.instructor_id

    def can_view(self, viewer: User, course: Course) -> bool:
        if self.can_edit(viewer, course):
            return True
        if course.status is PublishStatus.PUBLISHED:
            return True
        if course.status is PublishStatus.ARCHIVED and self._enrollments is not None:
            return self._enrollments.is_enrolled(course.id, viewer.id)
        return False

    def can_grade(self, viewer: User, course: Course) -> bool:
        return self.can_edit(viewer, course)

    def require_view(self, viewer: User, course: Course) -> None:
        if self.can_view(viewer, course):
            return
        if course.status is PublishStatus.DRAFT:
            raise NotPublishedError(f"course {course.id} is a draft; {viewer.id} cannot open it")
        raise PermissionDeniedError(f"{viewer.id} cannot open course {course.id}")

    def require_edit(self, viewer: User, course: Course) -> None:
        if not self.can_edit(viewer, course):
            raise PermissionDeniedError(f"{viewer.id} cannot edit course {course.id}")

    def require_grade(self, viewer: User, course: Course) -> None:
        if not self.can_grade(viewer, course):
            raise PermissionDeniedError(f"{viewer.id} cannot grade work in course {course.id}")


class CourseAccessProxy(ContentNode):
    """Protection Proxy: the same interface as ``Course``, checked on every access.

    The check runs per call, not once in the constructor, because a course can be
    archived - or a student unenrolled - between two reads of the same handle.
    """

    def __init__(self, course: Course, viewer: User, permissions: PermissionService) -> None:
        super().__init__(course.id, course.title)
        self._course = course
        self._viewer = viewer
        self._permissions = permissions

    def accept(self, visitor: ContentVisitor) -> Any:
        self._guard()
        return self._course.accept(visitor)

    def children(self) -> list[ContentNode]:
        self._guard()
        return self._course.children()

    def lessons(self) -> Iterator[Lesson]:
        self._guard()
        return self._course.lessons()

    def find(self, node_id: str) -> ContentNode:
        self._guard()
        return self._course.find(node_id)

    @property
    def status(self) -> PublishStatus:
        return self._course.status

    @property
    def subject(self) -> Course:
        """The real course, for services that have already checked the rights."""
        self._guard()
        return self._course

    def _guard(self) -> None:
        self._permissions.require_view(self._viewer, self._course)


# --8<-- [end:permissions]


# --8<-- [start:notifications]
class LearningObserver(Protocol):
    def on_learning_event(self, event: str, recipients: Sequence[str], detail: str) -> None: ...


class NotificationService:
    def __init__(self) -> None:
        self._inboxes: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def on_learning_event(self, event: str, recipients: Sequence[str], detail: str) -> None:
        with self._lock:
            for user_id in recipients:
                self._inboxes.setdefault(user_id, []).append(f"{event}: {detail}")

    def inbox(self, user_id: str) -> list[str]:
        with self._lock:
            return list(self._inboxes.get(user_id, []))

    def total(self) -> int:
        with self._lock:
            return sum(len(messages) for messages in self._inboxes.values())


# --8<-- [end:notifications]


# --8<-- [start:enrollment]
class PaymentGateway(Protocol):
    def charge(self, student_id: str, amount: Money) -> bool: ...


class AlwaysApprovesGateway:
    def charge(self, student_id: str, amount: Money) -> bool:
        return True


class EnrollmentService:
    """Seats, prerequisites and the waiting list. One lock per course.

    The seat is claimed *before* the payment and given back if the payment fails, so
    two students racing for the last seat cannot both get it and a declined card
    cannot leave a seat locked up.
    """

    def __init__(
        self,
        catalog: CourseCatalog,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        payments: PaymentGateway | None = None,
        observers: Iterable[LearningObserver] = (),
        waitlist: bool = True,
    ) -> None:
        self._catalog = catalog
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("ENR")
        self._payments = payments or AlwaysApprovesGateway()
        self._observers = list(observers)
        self._waitlist = waitlist
        self._enrollments: dict[str, Enrollment] = {}
        self._course_locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def enroll(self, course: Course, student: User) -> Enrollment:
        if not course.is_open():
            raise NotPublishedError(f"course {course.id} is {course.status}; enrollment is closed")
        self._require_prerequisites(course, student)
        enrollment = self._claim_seat(course, student)
        if enrollment.status is EnrollmentStatus.WAITLISTED:
            self._announce("waitlisted", [student.id], f"{course.title} is full")
            return enrollment
        if not course.is_free() and not self._payments.charge(student.id, course.price):
            self.cancel(enrollment.id)  # the seat goes straight back to the pool
            raise PaymentDeclinedError(f"payment of {course.price} declined for {student.id}")
        with self._lock_for(course.id):
            enrollment.activate()
        self._announce("enrolled", [student.id, course.instructor_id], course.title)
        return enrollment

    def _claim_seat(self, course: Course, student: User) -> Enrollment:
        with self._lock_for(course.id):
            for existing in self._for_course_unlocked(course.id):
                if existing.student_id == student.id and existing.is_live():
                    raise AlreadyEnrolledError(f"{student.id} is already {existing.status} on {course.id}")
            taken = sum(1 for e in self._for_course_unlocked(course.id) if e.takes_a_seat())
            if taken >= course.capacity and not self._waitlist:
                raise CourseFullError(f"{course.title} is full ({course.capacity} seats)")
            status = EnrollmentStatus.PENDING if taken < course.capacity else EnrollmentStatus.WAITLISTED
            enrollment = Enrollment(
                id=self._ids.next_id(),
                course_id=course.id,
                student_id=student.id,
                status=status,
                enrolled_at=self._clock.now(),
                price=course.price,
            )
            self._enrollments[enrollment.id] = enrollment
            return enrollment

    def cancel(self, enrollment_id: str) -> Enrollment:
        """Free the seat and promote the student who has waited longest."""
        enrollment = self.get(enrollment_id)
        course = self._catalog.get(enrollment.course_id)
        with self._lock_for(course.id):
            enrollment.status = EnrollmentStatus.CANCELLED
            waiting = sorted(
                (e for e in self._for_course_unlocked(course.id) if e.status is EnrollmentStatus.WAITLISTED),
                key=lambda e: (e.enrolled_at, e.id),
            )
            promoted = waiting[0] if waiting else None
            if promoted is not None:
                promoted.status = (
                    EnrollmentStatus.ACTIVE if course.is_free() else EnrollmentStatus.PENDING
                )
        if promoted is not None:
            self._announce("promoted from the waiting list", [promoted.student_id], course.title)
        return enrollment

    def get(self, enrollment_id: str) -> Enrollment:
        with self._registry_lock:
            enrollment = self._enrollments.get(enrollment_id)
        if enrollment is None:
            raise ContentNotFoundError(f"unknown enrollment {enrollment_id}")
        return enrollment

    def for_course(self, course_id: str) -> list[Enrollment]:
        with self._registry_lock:
            return [e for e in self._enrollments.values() if e.course_id == course_id]

    def active(self, course_id: str, student_id: str) -> Enrollment:
        for enrollment in self.for_course(course_id):
            if enrollment.student_id == student_id and enrollment.status in (
                EnrollmentStatus.ACTIVE,
                EnrollmentStatus.COMPLETED,
            ):
                return enrollment
        raise ContentNotFoundError(f"{student_id} has no active enrollment on {course_id}")

    def is_enrolled(self, course_id: str, student_id: str) -> bool:
        return any(
            e.student_id == student_id and e.is_live() for e in self.for_course(course_id)
        )

    def counts(self, course_id: str) -> dict[EnrollmentStatus, int]:
        counts: dict[EnrollmentStatus, int] = {}
        for enrollment in self.for_course(course_id):
            counts[enrollment.status] = counts.get(enrollment.status, 0) + 1
        return counts

    def _require_prerequisites(self, course: Course, student: User) -> None:
        for prerequisite in course.prerequisites:
            done = any(
                e.student_id == student.id and e.status is EnrollmentStatus.COMPLETED
                for e in self.for_course(prerequisite)
            )
            if not done:
                raise PrerequisiteError(f"{student.id} has not completed {prerequisite}")

    def _for_course_unlocked(self, course_id: str) -> list[Enrollment]:
        return [e for e in list(self._enrollments.values()) if e.course_id == course_id]

    def _lock_for(self, course_id: str) -> threading.Lock:
        with self._registry_lock:
            return self._course_locks.setdefault(course_id, threading.Lock())

    def _announce(self, event: str, recipients: Sequence[str], detail: str) -> None:
        for observer in self._observers:  # outside the course lock
            observer.on_learning_event(event, recipients, detail)


def seats_left(service: EnrollmentService, course: Course) -> int:
    taken = sum(1 for e in service.for_course(course.id) if e.takes_a_seat())
    return max(0, course.capacity - taken)


# --8<-- [end:enrollment]
