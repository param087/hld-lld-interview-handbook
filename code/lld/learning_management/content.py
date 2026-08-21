"""The course tree: Composite, with a Visitor hook and a Factory for the leaf types.

A course contains modules, a module contains lessons, and a lesson is one of four
things. Callers walk the tree through ``accept`` and never switch on a type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, ClassVar

from common import Money, ValidationError
from lld.learning_management.models import (
    FREE,
    WORDS_PER_MINUTE,
    ContentNotFoundError,
    LessonKind,
    PublishStatus,
    Question,
)

if TYPE_CHECKING:  # pragma: no cover - the visitor is only needed for type hints
    from lld.learning_management.visitors import ContentVisitor


# --8<-- [start:composite]
class ContentNode(ABC):
    """Composite component. Everything in the tree answers these four questions."""

    def __init__(self, node_id: str, title: str) -> None:
        if not title.strip():
            raise ValidationError("content needs a title")
        self.id = node_id
        self.title = title

    @abstractmethod
    def accept(self, visitor: ContentVisitor) -> Any:
        """Double dispatch: the node names itself, the visitor decides what to do."""

    def children(self) -> list[ContentNode]:
        return []

    def lessons(self) -> Iterator[Lesson]:
        for child in self.children():
            yield from child.lessons()

    def find(self, node_id: str) -> ContentNode:
        for node in self.walk():
            if node.id == node_id:
                return node
        raise ContentNotFoundError(f"{node_id} is not in {self.id}")

    def walk(self) -> Iterator[ContentNode]:
        yield self
        for child in self.children():
            yield from child.walk()


class Lesson(ContentNode):
    """Composite leaf. ``duration_minutes`` is what the duration visitor sums."""

    kind: ClassVar[LessonKind]

    def __init__(self, node_id: str, title: str, duration_minutes: int) -> None:
        super().__init__(node_id, title)
        if duration_minutes < 0:
            raise ValidationError(f"lesson {node_id}: duration cannot be negative")
        self.duration_minutes = duration_minutes

    def lessons(self) -> Iterator[Lesson]:
        yield self

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.id!r})"


class Module(ContentNode):
    def __init__(self, node_id: str, title: str, lessons: list[Lesson] | None = None) -> None:
        super().__init__(node_id, title)
        self._lessons: list[Lesson] = list(lessons or [])

    def add(self, lesson: Lesson) -> Module:
        self._lessons.append(lesson)
        return self

    def children(self) -> list[ContentNode]:
        return list(self._lessons)

    def accept(self, visitor: ContentVisitor) -> Any:
        return visitor.visit_module(self)


class Course(ContentNode):
    """The composite root, plus the enrollment rules that belong to the course itself."""

    def __init__(
        self,
        node_id: str,
        title: str,
        instructor_id: str,
        capacity: int = 100,
        price: Money = FREE,
        prerequisites: tuple[str, ...] = (),
        modules: list[Module] | None = None,
    ) -> None:
        super().__init__(node_id, title)
        if capacity < 1:
            raise ValidationError(f"course {node_id}: capacity must be positive")
        self.instructor_id = instructor_id
        self.capacity = capacity
        self.price = price
        self.prerequisites = prerequisites
        self.status = PublishStatus.DRAFT
        self._modules: list[Module] = list(modules or [])

    def add(self, module: Module) -> Course:
        self._modules.append(module)
        return self

    def children(self) -> list[ContentNode]:
        return list(self._modules)

    def accept(self, visitor: ContentVisitor) -> Any:
        return visitor.visit_course(self)

    def is_free(self) -> bool:
        return self.price.is_zero()

    def is_open(self) -> bool:
        return self.status is PublishStatus.PUBLISHED


# --8<-- [end:composite]


# --8<-- [start:lessons]
class VideoLesson(Lesson):
    kind = LessonKind.VIDEO

    def __init__(self, node_id: str, title: str, duration_minutes: int, url: str = "") -> None:
        super().__init__(node_id, title, duration_minutes)
        self.url = url

    def accept(self, visitor: ContentVisitor) -> Any:
        return visitor.visit_video(self)


class ArticleLesson(Lesson):
    """Its duration is derived, not stored: reading speed times length."""

    kind = LessonKind.ARTICLE

    def __init__(self, node_id: str, title: str, word_count: int) -> None:
        super().__init__(node_id, title, max(1, round(word_count / WORDS_PER_MINUTE)))
        self.word_count = word_count

    def accept(self, visitor: ContentVisitor) -> Any:
        return visitor.visit_article(self)


class Quiz(Lesson):
    """Auto-graded: the answers are in the questions, so no human is involved."""

    kind = LessonKind.QUIZ

    def __init__(
        self,
        node_id: str,
        title: str,
        questions: list[Question],
        pass_mark: float = 70.0,
        max_attempts: int = 3,
        time_limit_minutes: int = 20,
    ) -> None:
        super().__init__(node_id, title, time_limit_minutes)
        if not questions:
            raise ValidationError(f"quiz {node_id} needs at least one question")
        self.questions = list(questions)
        self.pass_mark = pass_mark
        self.max_attempts = max_attempts
        self.time_limit_minutes = time_limit_minutes

    @property
    def max_points(self) -> int:
        return sum(question.points for question in self.questions)

    def accept(self, visitor: ContentVisitor) -> Any:
        return visitor.visit_quiz(self)


class Assignment(Lesson):
    """Manually graded: submitting is not finishing, an instructor has to grade it."""

    kind = LessonKind.ASSIGNMENT

    def __init__(
        self, node_id: str, title: str, max_points: int = 100, pass_mark: float = 50.0, effort_minutes: int = 60
    ) -> None:
        super().__init__(node_id, title, effort_minutes)
        self.max_points = max_points
        self.pass_mark = pass_mark

    def accept(self, visitor: ContentVisitor) -> Any:
        return visitor.visit_assignment(self)


class LessonFactory:
    """Factory: the catalogue import format is data, the lesson types are classes."""

    @staticmethod
    def create(kind: LessonKind | str, node_id: str, title: str, **options: Any) -> Lesson:
        builders = {
            LessonKind.VIDEO: lambda: VideoLesson(
                node_id, title, int(options.get("duration_minutes", 5)), str(options.get("url", ""))
            ),
            LessonKind.ARTICLE: lambda: ArticleLesson(node_id, title, int(options.get("word_count", 600))),
            LessonKind.QUIZ: lambda: Quiz(node_id, title, list(options.get("questions", []))),
            LessonKind.ASSIGNMENT: lambda: Assignment(node_id, title, int(options.get("max_points", 100))),
        }
        try:
            return builders[LessonKind(kind)]()
        except ValueError:
            raise ValidationError(f"unknown lesson kind {kind!r}") from None


# --8<-- [end:lessons]
