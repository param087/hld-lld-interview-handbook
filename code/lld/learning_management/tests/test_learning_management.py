from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, Money, SequentialIdGenerator, ValidationError
from lld.learning_management.content import (
    ArticleLesson,
    Assignment,
    Course,
    LessonFactory,
    Module,
    Quiz,
    VideoLesson,
)
from lld.learning_management.learning import LearningService
from lld.learning_management.models import (
    AlreadyEnrolledError,
    AttemptLimitError,
    AttemptWindowError,
    CourseFullError,
    EnrollmentStatus,
    LessonKind,
    NotPublishedError,
    PaymentDeclinedError,
    PermissionDeniedError,
    PrerequisiteError,
    PublishStatus,
    Question,
    Role,
    User,
)
from lld.learning_management.services import (
    CourseCatalog,
    EnrollmentService,
    NotificationService,
    PermissionService,
    seats_left,
)
from lld.learning_management.visitors import DurationVisitor, ProgressVisitor

INSTRUCTOR = User("grace", "Grace", Role.INSTRUCTOR)
ADMIN = User("root", "Root", Role.ADMIN)
ADA = User("ada", "Ada")
QUESTIONS = [
    Question("q1", "2 + 2", ("3", "4"), 1, points=2),
    Question("q2", "Composite is a?", ("tree", "queue"), 0),
]


class Declines:
    def charge(self, student_id: str, amount: Money) -> bool:
        return False


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_760_000_000)


def make_course(course_id: str = "C-1", capacity: int = 10, price: str = "0.00", **kwargs: object) -> Course:
    course = Course(course_id, "Patterns", INSTRUCTOR.id, capacity=capacity, price=Money.of(price), **kwargs)
    course.add(
        Module(f"{course_id}-M1", "Foundations")
        .add(VideoLesson(f"{course_id}-L1", "Why patterns", 12))
        .add(ArticleLesson(f"{course_id}-L2", "SOLID", 1200))
    )
    course.add(
        Module(f"{course_id}-M2", "Assessment")
        .add(Quiz(f"{course_id}-L3", "Quiz", QUESTIONS, pass_mark=60.0, max_attempts=2, time_limit_minutes=10))
        .add(Assignment(f"{course_id}-L4", "Refactor", max_points=50, pass_mark=50.0))
    )
    return course


def platform(clock: FakeClock, **kwargs: object) -> tuple[CourseCatalog, EnrollmentService, LearningService]:
    catalog = CourseCatalog()
    ids = SequentialIdGenerator("ID")
    enrollments = EnrollmentService(catalog, clock=clock, ids=ids, **kwargs)
    permissions = PermissionService(enrollments)
    learning = LearningService(
        catalog, enrollments, permissions, clock=clock, ids=ids, observers=[NotificationService()]
    )
    return catalog, enrollments, learning


def published(catalog: CourseCatalog, course: Course) -> Course:
    catalog.add(course)
    catalog.set_status(course.id, PublishStatus.PUBLISHED)
    return course


def test_visitors_report_duration_and_progress(clock: FakeClock) -> None:
    course = make_course()
    assert course.accept(DurationVisitor()) == 12 + 6 + 10 + 60  # video, article, quiz, assignment
    progress = course.accept(ProgressVisitor({"C-1-L1", "C-1-L2"}))
    assert (progress.completed, progress.total, progress.percent) == (2, 4, 50.0)
    assert progress.minutes_done == 18 and not progress.is_finished()


# --8<-- [start:permissions]
def test_a_draft_is_invisible_to_students_but_open_to_its_instructor(clock: FakeClock) -> None:
    catalog, _, learning = platform(clock)
    course = catalog.add(make_course())

    with pytest.raises(NotPublishedError):
        learning.open_course(course.id, ADA).children()
    assert learning.open_course(course.id, INSTRUCTOR).children() != []
    assert learning.open_course(course.id, ADMIN).children() != []

    catalog.set_status(course.id, PublishStatus.PUBLISHED)
    assert learning.open_course(course.id, ADA).children() != []  # the same handle, new answer


# --8<-- [end:permissions]


def test_only_a_grader_may_grade(clock: FakeClock) -> None:
    catalog, enrollments, learning = platform(clock)
    course = published(catalog, make_course())
    enrollments.enroll(course, ADA)
    submission = learning.submit_assignment(course.id, ADA, "C-1-L4", "my answer")
    with pytest.raises(PermissionDeniedError):
        learning.grade_submission(course.id, ADA, submission.id, 50)
    assert learning.grade_submission(course.id, INSTRUCTOR, submission.id, 40).passed


def test_prerequisites_must_be_completed_first(clock: FakeClock) -> None:
    catalog, enrollments, learning = platform(clock)
    basics = published(catalog, make_course("C-0", capacity=5))
    advanced = published(catalog, make_course("C-1", prerequisites=("C-0",)))

    with pytest.raises(PrerequisiteError, match="C-0"):
        enrollments.enroll(advanced, ADA)
    enrollments.enroll(basics, ADA)
    for lesson in ("C-0-L1", "C-0-L2", "C-0-L3", "C-0-L4"):
        learning.complete_lesson(basics.id, ADA, lesson)
    assert enrollments.enroll(advanced, ADA).status is EnrollmentStatus.ACTIVE


def test_quiz_is_auto_graded_while_an_assignment_waits_for_a_human(clock: FakeClock) -> None:
    catalog, enrollments, learning = platform(clock)
    course = published(catalog, make_course())
    enrollments.enroll(course, ADA)

    attempt = learning.start_attempt(course.id, ADA, "C-1-L3")
    grade = learning.submit_attempt(course.id, ADA, attempt.id, {"q1": 1, "q2": 0})
    assert (grade.points, grade.max_points, grade.passed, grade.graded_by) == (3, 3, True, "auto")
    assert learning.progress(course.id, ADA).completed == 1  # passing completed the quiz

    submission = learning.submit_assignment(course.id, ADA, "C-1-L4", "my answer")
    assert submission.grade is None and learning.progress(course.id, ADA).completed == 1
    learning.grade_submission(course.id, INSTRUCTOR, submission.id, 45)
    assert learning.progress(course.id, ADA).completed == 2


def test_attempt_limit_and_time_window(clock: FakeClock) -> None:
    catalog, enrollments, learning = platform(clock)
    course = published(catalog, make_course())
    enrollments.enroll(course, ADA)

    late = learning.start_attempt(course.id, ADA, "C-1-L3")
    clock.advance(11 * 60)  # the quiz allows 10 minutes
    with pytest.raises(AttemptWindowError):
        learning.submit_attempt(course.id, ADA, late.id, {"q1": 1, "q2": 0})

    second = learning.start_attempt(course.id, ADA, "C-1-L3")
    learning.submit_attempt(course.id, ADA, second.id, {"q1": 0, "q2": 0})  # 1 of 3 points, fails
    with pytest.raises(AttemptLimitError, match="all 2 attempts"):
        learning.start_attempt(course.id, ADA, "C-1-L3")


def test_editing_a_published_course_lowers_progress_without_losing_data(clock: FakeClock) -> None:
    catalog, enrollments, learning = platform(clock)
    course = published(catalog, make_course())
    enrollments.enroll(course, ADA)
    for lesson in ("C-1-L1", "C-1-L2", "C-1-L3", "C-1-L4"):
        learning.complete_lesson(course.id, ADA, lesson)
    assert learning.progress(course.id, ADA).percent == 100.0

    module = course.find("C-1-M1")
    assert isinstance(module, Module)
    module.add(VideoLesson("C-1-L5", "New chapter", 10))
    assert learning.progress(course.id, ADA).percent == 80.0  # 4 of 5, nothing was lost

    learning.complete_lesson(course.id, ADA, "C-1-L5")
    assert learning.progress(course.id, ADA).percent == 100.0


def test_certificate_is_issued_once_when_the_last_lesson_lands(clock: FakeClock) -> None:
    catalog, enrollments, learning = platform(clock)
    course = published(catalog, make_course())
    enrollment = enrollments.enroll(course, ADA)
    lessons = ["C-1-L1", "C-1-L2", "C-1-L3", "C-1-L4"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda lesson: learning.complete_lesson(course.id, ADA, lesson), lessons))

    assert learning.progress(course.id, ADA).is_finished()
    assert enrollment.status is EnrollmentStatus.COMPLETED
    certificate = learning.certificate(course.id, ADA.id)
    assert certificate is not None and certificate.score_percent == 100.0


# --8<-- [start:capacity]
def test_a_full_course_hands_out_exactly_capacity_seats(clock: FakeClock) -> None:
    catalog, enrollments, _ = platform(clock)
    course = published(catalog, make_course(capacity=5))
    students = [User(f"s{i}", f"Student {i}") for i in range(20)]

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = [e.status for e in pool.map(lambda s: enrollments.enroll(course, s), students)]

    assert statuses.count(EnrollmentStatus.ACTIVE) == 5  # capacity, never six
    assert statuses.count(EnrollmentStatus.WAITLISTED) == 15
    with pytest.raises(AlreadyEnrolledError):
        enrollments.enroll(course, students[0])


# --8<-- [end:capacity]


def test_cancelling_promotes_the_student_who_waited_longest(clock: FakeClock) -> None:
    catalog, enrollments, _ = platform(clock)
    course = published(catalog, make_course(capacity=1))
    first = enrollments.enroll(course, ADA)
    waiting = enrollments.enroll(course, User("bob", "Bob"))
    assert waiting.status is EnrollmentStatus.WAITLISTED

    enrollments.cancel(first.id)
    assert waiting.status is EnrollmentStatus.ACTIVE  # free course, so straight to active


def test_a_declined_payment_gives_the_seat_back(clock: FakeClock) -> None:
    catalog, enrollments, _ = platform(clock, payments=Declines())
    course = published(catalog, make_course(capacity=1, price="49.00"))
    with pytest.raises(PaymentDeclinedError):
        enrollments.enroll(course, ADA)
    assert enrollments.counts(course.id).get(EnrollmentStatus.ACTIVE, 0) == 0
    assert seats_left(enrollments, course) == 1  # the held seat came straight back


@pytest.mark.parametrize(
    ("kind", "options", "expected_minutes"),
    [
        (LessonKind.VIDEO, {"duration_minutes": 9}, 9),
        (LessonKind.ARTICLE, {"word_count": 400}, 2),
        (LessonKind.ASSIGNMENT, {"max_points": 20}, 60),
    ],
)
def test_lesson_factory_builds_each_kind(kind: LessonKind, options: dict[str, int], expected_minutes: int) -> None:
    lesson = LessonFactory.create(kind, "L-9", "Lesson", **options)
    assert lesson.kind is kind and lesson.duration_minutes == expected_minutes


def test_validation_rejects_impossible_content(clock: FakeClock) -> None:
    with pytest.raises(ValidationError):
        Course("C-9", "Bad", INSTRUCTOR.id, capacity=0)
    with pytest.raises(ValidationError):
        Quiz("L-9", "Empty quiz", [])
    with pytest.raises(ValidationError):
        Question("q9", "Broken", ("a", "b"), correct_index=5)
    catalog, enrollments, _ = platform(clock, waitlist=False)
    course = published(catalog, make_course(capacity=1))
    enrollments.enroll(course, ADA)
    with pytest.raises(CourseFullError):
        enrollments.enroll(course, User("bob", "Bob"))
