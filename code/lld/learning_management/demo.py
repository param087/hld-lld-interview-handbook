"""One course, three students: a draft nobody can see, a full class, and a certificate."""

from common import FakeClock, HandbookError, Money, SequentialIdGenerator
from lld.learning_management.content import (
    ArticleLesson,
    Assignment,
    Course,
    Module,
    Quiz,
    VideoLesson,
)
from lld.learning_management.learning import LearningService
from lld.learning_management.models import (
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
from lld.learning_management.visitors import DurationVisitor, OutlineVisitor

INSTRUCTOR = User("grace", "Grace", Role.INSTRUCTOR)
ADA = User("ada", "Ada")
LINUS = User("linus", "Linus")
MARIE = User("marie", "Marie")

QUESTIONS = [
    Question("q1", "Which pattern adds behaviour to a tree?", ("Visitor", "Adapter"), 0, points=2),
    Question("q2", "Half-open intervals exclude which end?", ("start", "end"), 1),
]


def build_course() -> Course:
    course = Course("C-1", "Design patterns in Python", INSTRUCTOR.id, capacity=2, price=Money.of("49.00"))
    course.add(
        Module("M-1", "Foundations")
        .add(VideoLesson("L-1", "Why patterns", 12))
        .add(ArticleLesson("L-2", "SOLID in one page", 1200))
    )
    course.add(
        Module("M-2", "Assessment")
        .add(Quiz("L-3", "Patterns quiz", QUESTIONS, pass_mark=60.0, time_limit_minutes=10))
        .add(Assignment("L-4", "Refactor a god object", max_points=50, pass_mark=50.0))
    )
    return course


def main() -> None:
    clock = FakeClock(start=1_760_000_000)
    ids = SequentialIdGenerator("ID")
    catalog, inboxes = CourseCatalog(), NotificationService()
    enrollments = EnrollmentService(catalog, clock=clock, ids=ids, observers=[inboxes])
    permissions = PermissionService(enrollments)
    learning = LearningService(catalog, enrollments, permissions, clock=clock, ids=ids, observers=[inboxes])

    course = catalog.add(build_course())
    print(f"{course.id} {course.title}: {course.accept(DurationVisitor())} minutes, seats {course.capacity}")
    try:
        learning.open_course(course.id, ADA).children()
    except HandbookError as exc:
        print(f"draft is invisible: {exc}")

    catalog.set_status(course.id, PublishStatus.PUBLISHED)
    for student in (ADA, LINUS, MARIE):
        enrollment = enrollments.enroll(course, student)
        print(f"{student.name} -> {enrollment.status} (seats free: {seats_left(enrollments, course)})")

    print(learning.open_course(course.id, ADA).accept(OutlineVisitor()))
    learning.complete_lesson(course.id, ADA, "L-1")
    learning.complete_lesson(course.id, ADA, "L-2")
    print(f"Ada after two lessons: {learning.progress(course.id, ADA)}")

    attempt = learning.start_attempt(course.id, ADA, "L-3")
    clock.advance(5 * 60)
    grade = learning.submit_attempt(course.id, ADA, attempt.id, {"q1": 0, "q2": 1})
    print(f"quiz auto-graded: {grade}; progress {learning.progress(course.id, ADA)}")

    submission = learning.submit_assignment(course.id, ADA, "L-4", "extracted three collaborators")
    try:
        learning.grade_submission(course.id, LINUS, submission.id, 50)
    except HandbookError as exc:
        print(f"peer grading refused: {exc}")
    print(f"instructor grades it: {learning.grade_submission(course.id, INSTRUCTOR, submission.id, 45)}")

    certificate = learning.certificate(course.id, ADA.id)
    status = enrollments.active(course.id, ADA.id).status
    print(f"Ada is {status}: certificate {certificate.id} at {certificate.score_percent}%")
    enrollments.cancel(enrollments.active(course.id, LINUS.id).id)
    marie = next(e for e in enrollments.for_course(course.id) if e.student_id == MARIE.id)
    print(f"Linus cancels -> Marie moves off the waiting list to {marie.status}")
    print(f"{inboxes.total()} notifications, Ada has {len(inboxes.inbox(ADA.id))}")


if __name__ == "__main__":
    main()
