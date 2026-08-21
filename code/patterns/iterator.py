"""Iterator: visit the elements of a structure one at a time without exposing how it is stored.

The running example is a file tree. ``Directory`` (the Aggregate) is iterable over
its direct children; ``DepthFirstIterator`` (the classic Iterator) walks the whole
tree with an explicit stack and the two methods the iterator protocol needs.
The generator functions that follow do the same walks in a fraction of the code,
because a generator's frame is the iterator state you would otherwise write by
hand. ``paginate`` applies the idea to a remote source: the caller sees one lazy
sequence and never touches a cursor.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import islice
from typing import Protocol

from common import ValidationError


# --8<-- [start:tree]
@dataclass(frozen=True, slots=True)
class File:
    name: str
    size: int


@dataclass(slots=True)
class Directory:
    """The Aggregate: iterating a directory yields its direct children, in insertion order.

    ``__iter__`` returns a fresh iterator over a snapshot, so a directory can be
    walked twice, by two callers at once, and a child removed during the walk
    neither raises nor skips its neighbour.
    """

    name: str
    children: list[Node] = field(default_factory=list)

    def add[N: Node](self, node: N) -> N:
        if any(child.name == node.name for child in self.children):
            raise ValidationError(f"{self.name!r} already has a child named {node.name!r}")
        self.children.append(node)
        return node

    def remove(self, name: str) -> None:
        self.children = [child for child in self.children if child.name != name]

    def __iter__(self) -> Iterator[Node]:
        return iter(tuple(self.children))


type Node = File | Directory


class DepthFirstIterator:
    """The classic Iterator: pre-order traversal with an explicit stack.

    ``__iter__`` returns ``self`` because an iterator *is* its own iterable; that is
    also why it is one-shot. The stack is the traversal state a generator's frame
    would keep for you (compare ``walk_depth_first`` below).
    """

    def __init__(self, root: Directory) -> None:
        self._stack: list[Node] = list(reversed(tuple(root)))

    def __iter__(self) -> DepthFirstIterator:
        return self

    def __next__(self) -> Node:
        if not self._stack:
            raise StopIteration
        node = self._stack.pop()
        if isinstance(node, Directory):
            self._stack.extend(reversed(tuple(node)))
        return node


# --8<-- [end:tree]


# --8<-- [start:generators]
def walk_depth_first(directory: Directory) -> Iterator[Node]:
    """The same pre-order walk as ``DepthFirstIterator``: the call stack is the explicit stack."""
    for child in directory:
        yield child
        if isinstance(child, Directory):
            yield from walk_depth_first(child)


def walk_breadth_first(directory: Directory) -> Iterator[Node]:
    """Level by level; the queue is the only state, and it lives in the generator's frame."""
    queue: deque[Directory] = deque([directory])
    while queue:
        for child in queue.popleft():
            yield child
            if isinstance(child, Directory):
                queue.append(child)


def files_only(nodes: Iterator[Node]) -> Iterator[File]:
    """Iterators compose: a filter over any walk, evaluated only as the caller pulls."""
    return (node for node in nodes if isinstance(node, File))


# --8<-- [end:generators]


# --8<-- [start:pagination]
@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[str, ...]
    next_cursor: str | None


class PageSource(Protocol):
    """Anything that serves pages by cursor: an HTTP API, a database, the in-memory fake below."""

    def fetch(self, cursor: str | None, limit: int) -> Page: ...


def paginate(source: PageSource, limit: int) -> Iterator[str]:
    """Hide the cursor dance: the caller sees one sequence, pages are fetched only as consumed."""
    if limit < 1:
        raise ValidationError("page size must be at least 1")
    cursor: str | None = None
    while True:
        page = source.fetch(cursor, limit)
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


class ListSource:
    """An in-memory page source whose cursor is the offset; it counts its calls for the tests."""

    def __init__(self, items: list[str]) -> None:
        self._items = tuple(items)
        self.calls: list[str | None] = []

    def fetch(self, cursor: str | None, limit: int) -> Page:
        self.calls.append(cursor)
        start = int(cursor) if cursor else 0
        end = start + limit
        next_cursor = str(end) if end < len(self._items) else None
        return Page(self._items[start:end], next_cursor)


# --8<-- [end:pagination]


def sample_tree() -> Directory:
    root = Directory("root")
    docs = root.add(Directory("docs"))
    docs.add(File("intro.md", 1200))
    docs.add(File("api.md", 800))
    src = root.add(Directory("src"))
    src.add(File("main.py", 3000))
    src.add(File("util.py", 500))
    root.add(File("README", 100))
    return root


def _names(nodes: Iterator[Node]) -> str:
    return ", ".join(node.name for node in nodes)


def main() -> None:
    root = sample_tree()
    print("--- the tree: root/{docs/{intro.md, api.md}, src/{main.py, util.py}, README} ---")
    print(f"DepthFirstIterator:  {_names(DepthFirstIterator(root))}")
    print(f"walk_depth_first:    {_names(walk_depth_first(root))}")
    print(f"walk_breadth_first:  {_names(walk_breadth_first(root))}")
    same = list(DepthFirstIterator(root)) == list(walk_depth_first(root))
    print(f"class and generator agree on the order: {same}")

    print("--- the caller composes with itertools and never sees the tree ---")
    print(f"first three files:   {_names(islice(files_only(walk_depth_first(root)), 3))}")
    total = sum(node.size for node in files_only(walk_depth_first(root)))
    print(f"total size:          {total} bytes")

    print("--- an iterable can be walked twice; an iterator is one-shot ---")
    print(f"root children twice: {len(list(root))} then {len(list(root))}")
    walker = DepthFirstIterator(root)
    print(f"same iterator twice: {len(list(walker))} then {len(list(walker))}")

    print("--- paginate hides the cursor; pages are fetched only as consumed ---")
    source = ListSource(["alice", "bob", "carol", "dave", "erin", "frank", "grace"])
    first_three = list(islice(paginate(source, limit=2), 3))
    print(f"first three members: {', '.join(first_three)} ({len(source.calls)} fetches of 2)")
    everyone = list(paginate(source, limit=3))
    print(f"all {len(everyone)} members:       {len(source.calls) - 2} more fetches of 3, cursors {source.calls[2:]}")


if __name__ == "__main__":
    main()
