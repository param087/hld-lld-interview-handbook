"""Grading strategies and the student-facing service: progress, quizzes, certificates."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from typing import Protocol

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock, ValidationError
from lld.learning_management.content import Assignment, Course, Lesson, Quiz
from lld.learning_management.models import (
    AttemptLimitError,
    AttemptWindowError,
    Certificate,
    ContentNotFoundError,
    Enrollment,
    EnrollmentStatus,
    Grade,
    Progress,
    QuizAttempt,
    Submission,
    SubmissionStatus,
    User,
)
from lld.learning_management.services import (
    CourseAccessProxy,
    CourseCatalog,
    EnrollmentService,
    LearningObserver,
    PermissionService,
)
from lld.learning_management.visitors import ProgressVisitor

SECONDS_PER_MINUTE = 60


# --8<-- [start:grading]
class GradingStrategy(Protocol):
    """Grade the work, or return None to say "a human has to look at this"."""

    def grade(self, lesson: Lesson, work: object, at: float) -> Grade | None: ...


class AutoGrader:
    """Quizzes: the answer key is in the questions, so grading is a comparison."""

    def grade(self, lesson: Lesson, work: object, at: float) -> Grade:
        if not isinstance(lesson, Quiz) or not isinstance(work, QuizAttempt):
            raise ValidationError("AutoGrader only grades quiz attempts")
        earned = sum(q.points for q in lesson.questions if q.is_correct(work.answers.get(q.id)))
        percent = 0.0 if lesson.max_points == 0 else 100 * earned / lesson.max_points
        return Grade(
            points=earned,
            max_points=lesson.max_points,
            graded_by="auto",
            graded_at=at,
            passed=percent >= lesson.pass_mark,
        )


class ManualGrader:
    """Assignments: submitting is not finishing. ``grade`` returns None on purpose."""

    def grade(self, lesson: Lesson, work: object, at: float) -> Grade | None:
        return None

    def apply(self, assignment: Assignment, points: int, grader_id: str, at: float) -> Grade:
        if not 0 <= points <= assignment.max_points:
            raise ValidationError(f"points must be between 0 and {assignment.max_points}")
        percent = 100 * points / assignment.max_points if assignment.max_points else 0.0
        return Grade(points, assignment.max_points, grader_id, at, percent >= assignment.pass_mark)


def grading_for(lesson: Lesson) -> GradingStrategy:
    """Factory: the lesson type decides who grades it."""
    return AutoGrader() if isinstance(lesson, Quiz) else ManualGrader()


# --8<-- [end:grading]


# --8<-- [start:learning]
class LearningService:
    """What a student does after enrolling. One lock per enrollment.

    That lock is what makes "the last lesson issues exactly one certificate" true when
    two devices finish two lessons at the same moment.
    """

    def __init__(
        self,
        catalog: CourseCatalog,
        enrollments: EnrollmentService,
        permissions: PermissionService,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        observers: Iterable[LearningObserver] = (),
    ) -> None:
        self._catalog = catalog
        self._enrollments = enrollments
        self._permissions = permissions
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("LRN")
        self._observers = list(observers)
        self._attempts: dict[str, QuizAttempt] = {}
        self._submissions: dict[str, Submission] = {}
        self._certificates: dict[str, Certificate] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._registry_lock = threading.Lock()

    # -- reading ------------------------------------------------------------------------
    def open_course(self, course_id: str, viewer: User) -> CourseAccessProxy:
        """Hand back a Proxy, never the Course itself."""
        return CourseAccessProxy(self._catalog.get(course_id), viewer, self._permissions)

    def progress(self, course_id: str, student: User) -> Progress:
        enrollment = self._enrollments.active(course_id, student.id)
        course = self.open_course(course_id, student).subject
        return self._progress_of(course, enrollment)

    def certificate(self, course_id: str, student_id: str) -> Certificate | None:
        with self._registry_lock:
            return self._certificates.get(f"{course_id}:{student_id}")

    # -- doing --------------------------------------------------------------------------
    def complete_lesson(self, course_id: str, student: User, lesson_id: str) -> Progress:
        course = self.open_course(course_id, student).subject  # permission check
        if not isinstance(course.find(lesson_id), Lesson):
            raise ContentNotFoundError(f"{lesson_id} is not a lesson")
        return self._mark_complete(course, student.id, lesson_id)

    def _mark_complete(self, course: Course, student_id: str, lesson_id: str) -> Progress:
        """Record it, recompute, and issue the certificate - all under one lock."""
        enrollment = self._enrollments.active(course.id, student_id)
        with self._lock_for(enrollment.id):
            enrollment.completed_lesson_ids.add(lesson_id)
            progress = self._progress_of(course, enrollment)
            if progress.is_finished() and enrollment.status is EnrollmentStatus.ACTIVE:
                self._finish(course, enrollment, progress)
        return progress

    def start_attempt(self, course_id: str, student: User, quiz_id: str) -> QuizAttempt:
        quiz = self._lesson(course_id, student, quiz_id, Quiz)
        self._enrollments.active(course_id, student.id)
        attempt = QuizAttempt(self._ids.next_id(), quiz_id, student.id, self._clock.now())
        with self._registry_lock:
            used = sum(1 for a in self._attempts.values() if a.quiz_id == quiz_id and a.student_id == student.id)
            if used >= quiz.max_attempts:
                raise AttemptLimitError(f"{student.id} has used all {quiz.max_attempts} attempts on {quiz_id}")
            self._attempts[attempt.id] = attempt
        return attempt

    def submit_attempt(self, course_id: str, student: User, attempt_id: str, answers: dict[str, int]) -> Grade:
        attempt = self._attempts[attempt_id]
        quiz = self._lesson(course_id, student, attempt.quiz_id, Quiz)
        now = self._clock.now()
        if now - attempt.started_at > quiz.time_limit_minutes * SECONDS_PER_MINUTE:
            attempt.submitted_at = now
            raise AttemptWindowError(f"attempt {attempt_id} ran past the {quiz.time_limit_minutes} minute limit")
        attempt.answers, attempt.submitted_at = dict(answers), now
        grade = AutoGrader().grade(quiz, attempt, now)
        attempt.grade = grade
        if grade.passed:
            self._mark_complete(self._catalog.get(course_id), student.id, quiz.id)
        self._announce("quiz graded", [student.id], f"{quiz.title}: {grade}")
        return grade

    def submit_assignment(self, course_id: str, student: User, assignment_id: str, text: str) -> Submission:
        assignment = self._lesson(course_id, student, assignment_id, Assignment)
        self._enrollments.active(course_id, student.id)
        submission = Submission(self._ids.next_id(), assignment.id, student.id, text, self._clock.now())
        with self._registry_lock:
            self._submissions[submission.id] = submission
        course = self._catalog.get(course_id)
        self._announce("assignment submitted", [course.instructor_id], f"{assignment.title} by {student.id}")
        return submission

    def grade_submission(self, course_id: str, grader: User, submission_id: str, points: int) -> Grade:
        course = self._catalog.get(course_id)
        self._permissions.require_grade(grader, course)  # students cannot grade themselves
        submission = self._submissions[submission_id]
        assignment = course.find(submission.assignment_id)
        if not isinstance(assignment, Assignment):
            raise ContentNotFoundError(f"{submission.assignment_id} is not an assignment")
        grade = ManualGrader().apply(assignment, points, grader.id, self._clock.now())
        submission.grade, submission.status = grade, SubmissionStatus.GRADED
        if grade.passed:
            self._mark_complete(course, submission.student_id, assignment.id)
        self._announce("assignment graded", [submission.student_id], f"{assignment.title}: {grade}")
        return grade

    # -- internals ----------------------------------------------------------------------
    def _lesson(self, course_id: str, viewer: User, lesson_id: str, expected: type[Lesson]) -> Lesson:
        node = self.open_course(course_id, viewer).find(lesson_id)
        if not isinstance(node, expected):
            raise ContentNotFoundError(f"{lesson_id} is not a {expected.__name__.lower()}")
        return node

    def _progress_of(self, course: Course, enrollment: Enrollment) -> Progress:
        return course.accept(ProgressVisitor(set(enrollment.completed_lesson_ids)))

    def _finish(self, course: Course, enrollment: Enrollment, progress: Progress) -> None:
        enrollment.status = EnrollmentStatus.COMPLETED
        enrollment.completed_at = self._clock.now()
        certificate = Certificate(
            id=self._ids.next_id(),
            course_id=course.id,
            student_id=enrollment.student_id,
            issued_at=enrollment.completed_at,
            score_percent=progress.percent,
        )
        with self._registry_lock:
            self._certificates[f"{course.id}:{enrollment.student_id}"] = certificate
        self._announce("certificate issued", [enrollment.student_id], course.title)

    def _lock_for(self, enrollment_id: str) -> threading.RLock:
        with self._registry_lock:
            return self._locks.setdefault(enrollment_id, threading.RLock())

    def _announce(self, event: str, recipients: Sequence[str], detail: str) -> None:
        for observer in self._observers:
            observer.on_learning_event(event, recipients, detail)


# --8<-- [end:learning]
