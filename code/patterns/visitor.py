"""Visitor: add an operation over an object structure without changing the structure's classes.

The running example is a file tree. ``File`` and ``Directory`` (the Elements) know
how to ``accept`` a visitor and nothing about sizes or searches; ``SizeVisitor``
and ``SearchVisitor`` (the Visitors) each add one operation by implementing
``visit_file`` and ``visit_directory``. The last section restates the idea with
``functools.singledispatch`` and a ``match`` statement, the Pythonic forms that
dispatch on the element's type without an ``accept`` method at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from fnmatch import fnmatch
from functools import singledispatch
from typing import Protocol

from common import ConflictError, ValidationError


# --8<-- [start:elements]
class Visitor[R](Protocol):
    """The Visitor interface: one method per concrete element, all returning ``R``."""

    def visit_file(self, file: File) -> R: ...

    def visit_directory(self, directory: Directory) -> R: ...


class Node(ABC):
    """The Element: knows its name and how to hand itself to a visitor, nothing else."""

    def __init__(self, name: str) -> None:
        if not name or "/" in name:
            raise ValidationError(f"invalid node name {name!r}")
        self.name = name

    @abstractmethod
    def accept[R](self, visitor: Visitor[R]) -> R: ...


class File(Node):
    def __init__(self, name: str, size: int) -> None:
        super().__init__(name)
        if size < 0:
            raise ValidationError("size cannot be negative")
        self.size = size

    def accept[R](self, visitor: Visitor[R]) -> R:
        return visitor.visit_file(self)  # the second dispatch: chosen by the element's type


class Directory(Node):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._children: dict[str, Node] = {}

    @property
    def children(self) -> list[Node]:
        return list(self._children.values())

    def add[N: Node](self, child: N) -> N:
        if child.name in self._children:
            raise ConflictError(f"{child.name!r} already exists in {self.name!r}")
        self._children[child.name] = child
        return child

    def accept[R](self, visitor: Visitor[R]) -> R:
        return visitor.visit_directory(self)


# --8<-- [end:elements]


# --8<-- [start:visitors]
class SizeVisitor:
    """Total bytes under a node. Stateless, so one instance serves any number of trees."""

    def visit_file(self, file: File) -> int:
        return file.size

    def visit_directory(self, directory: Directory) -> int:
        return sum(child.accept(self) for child in directory.children)  # the visitor drives traversal


class SearchVisitor:
    """Paths of the files whose name matches a glob pattern, in traversal order.

    The visitor owns the traversal *and* a path stack, state the elements never
    needed: the operation carries what it needs and leaves the tree alone. The
    stack is restored on the way out, so one visitor can be reused.
    """

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self._path: list[str] = []

    def visit_file(self, file: File) -> list[str]:
        if fnmatch(file.name, self.pattern):
            return ["/".join([*self._path, file.name])]
        return []

    def visit_directory(self, directory: Directory) -> list[str]:
        self._path.append(directory.name)
        try:
            return [path for child in directory.children for path in child.accept(self)]
        finally:
            self._path.pop()


# --8<-- [end:visitors]


# --8<-- [start:functional]
@singledispatch
def size_of(node: Node) -> int:
    """The Pythonic visitor: the library dispatches on ``type(node)``; no ``accept`` needed."""
    raise TypeError(f"no size rule for {type(node).__name__}")


@size_of.register
def _size_of_file(node: File) -> int:
    return node.size


@size_of.register
def _size_of_directory(node: Directory) -> int:
    return sum(size_of(child) for child in node.children)


def find(node: Node, pattern: str, prefix: str = "") -> list[str]:
    """``match`` on the element type: every case on one screen, the last one catching strangers."""
    path = f"{prefix}/{node.name}" if prefix else node.name
    match node:
        case File(name=name) if fnmatch(name, pattern):
            return [path]
        case File():
            return []
        case Directory(children=children):
            return [hit for child in children for hit in find(child, pattern, path)]
        case _:
            raise TypeError(f"no search rule for {type(node).__name__}")


# --8<-- [end:functional]


def build_tree() -> Directory:
    home = Directory("home")
    docs = home.add(Directory("docs"))
    docs.add(File("guide.md", 1_200))
    docs.add(File("notes.txt", 300))
    src = home.add(Directory("src"))
    src.add(File("main.py", 2_400))
    src.add(File("README.md", 800))
    pkg = src.add(Directory("pkg"))
    pkg.add(File("__init__.py", 0))
    pkg.add(File("core.py", 2_000))
    home.add(Directory("empty"))
    return home


def main() -> None:
    class OutlineVisitor:
        """A third operation, written for the demo: the tree still has no print method."""

        def __init__(self) -> None:
            self.depth = 0

        def visit_file(self, file: File) -> list[str]:
            return [f"{'  ' * self.depth}{file.name} ({file.size} B)"]

        def visit_directory(self, directory: Directory) -> list[str]:
            lines = [f"{'  ' * self.depth}{directory.name}/"]
            self.depth += 1
            for child in directory.children:
                lines.extend(child.accept(self))
            self.depth -= 1
            return lines

    home = build_tree()
    print("--- the tree, drawn by a visitor: no node has a print method ---")
    for line in home.accept(OutlineVisitor()):
        print(f"  {line}")

    print("--- SizeVisitor: one operation, no size() on any node ---")
    sizes = SizeVisitor()
    for node in (home, *home.children):
        print(f"{node.name:<6}= {node.accept(sizes)} B")

    print("--- SearchVisitor: a second operation, the tree untouched ---")
    for pattern in ("*.md", "*.py"):
        print(f"{pattern:<5} -> {', '.join(home.accept(SearchVisitor(pattern)))}")

    print("--- the same operations with singledispatch and match ---")
    print(f"size_of(home) = {size_of(home)} B")
    print(f"find(home, '*.md') = {find(home, '*.md')}")
    try:
        home.add(Directory("docs"))
    except ConflictError as exc:
        print(f"rejected: {exc}")


if __name__ == "__main__":
    main()
