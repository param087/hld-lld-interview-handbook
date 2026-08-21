"""Visitors over the course tree: duration, progress and a printable outline.

Each of these would otherwise be a method on every node class. As a visitor, one
new question is one new class and the tree never changes.
"""

from __future__ import annotations

from typing import Protocol

from lld.learning_management.content import (
    ArticleLesson,
    Assignment,
    Course,
    Lesson,
    Module,
    Quiz,
    VideoLesson,
)
from lld.learning_management.models import Progress


# --8<-- [start:visitor]
class ContentVisitor(Protocol):
    """One method per node type: the tree is closed, the operations are open."""

    def visit_course(self, course: Course) -> object: ...

    def visit_module(self, module: Module) -> object: ...

    def visit_video(self, lesson: VideoLesson) -> object: ...

    def visit_article(self, lesson: ArticleLesson) -> object: ...

    def visit_quiz(self, quiz: Quiz) -> object: ...

    def visit_assignment(self, assignment: Assignment) -> object: ...


class DurationVisitor:
    """Total minutes. Composites add up their children, leaves report themselves."""

    def visit_course(self, course: Course) -> int:
        return sum(int(child.accept(self)) for child in course.children())

    def visit_module(self, module: Module) -> int:
        return sum(int(child.accept(self)) for child in module.children())

    def visit_video(self, lesson: VideoLesson) -> int:
        return lesson.duration_minutes

    def visit_article(self, lesson: ArticleLesson) -> int:
        return lesson.duration_minutes

    def visit_quiz(self, quiz: Quiz) -> int:
        return quiz.time_limit_minutes

    def visit_assignment(self, assignment: Assignment) -> int:
        return assignment.duration_minutes


class ProgressVisitor:
    """Counts lessons and minutes done against the tree *as it is now*.

    Completed ids for lessons that were removed are ignored rather than counted, so
    editing a published course can never push anyone over 100 percent.
    """

    def __init__(self, completed_lesson_ids: set[str]) -> None:
        self._done = completed_lesson_ids
        self.completed = 0
        self.total = 0
        self.minutes_done = 0
        self.minutes_total = 0

    def result(self) -> Progress:
        return Progress(self.completed, self.total, self.minutes_done, self.minutes_total)

    def visit_course(self, course: Course) -> Progress:
        for child in course.children():
            child.accept(self)
        return self.result()

    def visit_module(self, module: Module) -> Progress:
        for child in module.children():
            child.accept(self)
        return self.result()

    def _count(self, lesson: Lesson) -> Progress:
        self.total += 1
        self.minutes_total += lesson.duration_minutes
        if lesson.id in self._done:
            self.completed += 1
            self.minutes_done += lesson.duration_minutes
        return self.result()

    def visit_video(self, lesson: VideoLesson) -> Progress:
        return self._count(lesson)

    def visit_article(self, lesson: ArticleLesson) -> Progress:
        return self._count(lesson)

    def visit_quiz(self, quiz: Quiz) -> Progress:
        return self._count(quiz)

    def visit_assignment(self, assignment: Assignment) -> Progress:
        return self._count(assignment)


class OutlineVisitor:
    """Renders the syllabus. Proof that a third question needs no change to the tree."""

    def __init__(self, completed_lesson_ids: set[str] | None = None) -> None:
        self._done = completed_lesson_ids or set()
        self._lines: list[str] = []

    def text(self) -> str:
        return "\n".join(self._lines)

    def visit_course(self, course: Course) -> str:
        self._lines.append(f"{course.title} [{course.status}]")
        for child in course.children():
            child.accept(self)
        return self.text()

    def visit_module(self, module: Module) -> str:
        self._lines.append(f"  {module.title}")
        for child in module.children():
            child.accept(self)
        return self.text()

    def _leaf(self, lesson: Lesson) -> str:
        mark = "x" if lesson.id in self._done else " "
        self._lines.append(f"    [{mark}] {lesson.title} ({lesson.kind}, {lesson.duration_minutes} min)")
        return self.text()

    def visit_video(self, lesson: VideoLesson) -> str:
        return self._leaf(lesson)

    def visit_article(self, lesson: ArticleLesson) -> str:
        return self._leaf(lesson)

    def visit_quiz(self, quiz: Quiz) -> str:
        return self._leaf(quiz)

    def visit_assignment(self, assignment: Assignment) -> str:
        return self._leaf(assignment)


# --8<-- [end:visitor]
