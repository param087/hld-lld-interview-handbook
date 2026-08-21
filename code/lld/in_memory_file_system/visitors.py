"""Traversals that live outside the node classes.

Composite already gives ``size()`` for free. Visitor is for the operations you
do *not* want on ``Node``: search, reporting, rendering, quota accounting. Add a
tenth of those and the node classes stay exactly as they are.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from lld.in_memory_file_system.models import NodeStat, SizeReport
from lld.in_memory_file_system.nodes import Directory, File, Node


# --8<-- [start:protocol]
class NodeVisitor(Protocol):
    """Double dispatch: the node calls back the method for its own type."""

    def visit_file(self, file: File) -> None: ...
    def visit_directory(self, directory: Directory) -> None: ...


# --8<-- [end:protocol]


# --8<-- [start:visitors]
class SizeVisitor:
    """One traversal, four answers -- the reason this is not a method on Node."""

    def __init__(self) -> None:
        self.files = 0
        self.directories = 0
        self.total_bytes = 0
        self.largest_file: str | None = None
        self._largest = -1

    def visit_file(self, file: File) -> None:
        size = file.size()
        self.files += 1
        self.total_bytes += size
        if size > self._largest:
            self._largest, self.largest_file = size, file.path()

    def visit_directory(self, directory: Directory) -> None:
        self.directories += 1
        for child in directory.children():  # the visitor drives the recursion
            child.accept(self)

    def report(self) -> SizeReport:
        return SizeReport(self.files, self.directories, self.total_bytes, self.largest_file)


class SearchVisitor:
    """find(1) as an object. The predicate is the only thing that varies."""

    def __init__(self, predicate: Callable[[Node], bool]) -> None:
        self._predicate = predicate
        self.matches: list[str] = []

    def visit_file(self, file: File) -> None:
        if self._predicate(file):
            self.matches.append(file.path())

    def visit_directory(self, directory: Directory) -> None:
        if self._predicate(directory):
            self.matches.append(directory.path())
        for child in directory.children():
            child.accept(self)

    @staticmethod
    def by_name(name: str) -> SearchVisitor:
        return SearchVisitor(lambda node: node.name == name)

    @staticmethod
    def by_extension(extension: str) -> SearchVisitor:
        suffix = extension if extension.startswith(".") else f".{extension}"
        return SearchVisitor(lambda node: not node.is_directory() and node.name.endswith(suffix))

    @staticmethod
    def by_min_size(minimum: int) -> SearchVisitor:
        return SearchVisitor(lambda node: not node.is_directory() and node.size() >= minimum)


class TreeVisitor:
    """Renders ``tree(1)``. Purely presentational, and therefore not on Node."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._depth = 0

    def visit_file(self, file: File) -> None:
        self.lines.append(f"{'  ' * self._depth}{file.name} ({file.size()}B)")

    def visit_directory(self, directory: Directory) -> None:
        self.lines.append(f"{'  ' * self._depth}{directory.name or ''}/")
        self._depth += 1
        for child in directory.children():
            child.accept(self)
        self._depth -= 1

    def render(self) -> str:
        return "\n".join(self.lines)


class StatVisitor:
    """Collects a stat record per node -- the shape a listing API would return."""

    def __init__(self) -> None:
        self.stats: list[NodeStat] = []

    def visit_file(self, file: File) -> None:
        self.stats.append(file.stat())

    def visit_directory(self, directory: Directory) -> None:
        self.stats.append(directory.stat())
        for child in directory.children():
            child.accept(self)


# --8<-- [end:visitors]
