"""Composite: a tree whose leaves and branches answer the same questions.

The running example is a file tree. ``FileSystemNode`` (the Component) declares
what every node can do: report its ``size`` and its ``children`` and, built on
those two, iterate its subtree, search it and render it. ``File`` (the Leaf) has
no children. ``Directory`` (the Composite) holds nodes by name and adds the
operations only a container can have: ``add``, ``remove``, ``get`` and
``resolve``. A client that sums sizes or prints a tree never asks which kind of
node it is holding.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from operator import methodcaller

from common import ConflictError, NotFoundError, ValidationError

INDENT = "    "


# --8<-- [start:component]
class FileSystemNode(ABC):
    """The Component: the interface a leaf and a composite share.

    Two primitives are abstract, ``size`` and ``children``. Everything that means
    "visit every node" is written once here in terms of them, so a leaf gets the
    traversal for free and a client never branches on the node type.
    """

    name: str

    @abstractmethod
    def size(self) -> int:
        """Bytes in this node, including everything beneath it."""

    @abstractmethod
    def children(self) -> Sequence[FileSystemNode]:
        """Direct children; empty for a leaf, so callers never special-case it."""

    def __iter__(self) -> Iterator[FileSystemNode]:
        """Pre-order walk of the subtree, the node itself first."""
        yield self
        for child in self.children():
            yield from child

    def find(self, predicate: Callable[[FileSystemNode], bool]) -> Iterator[FileSystemNode]:
        return (node for node in self if predicate(node))

    def label(self) -> str:
        return f"{self.name} ({self.size()} B)"

    def render(self, depth: int = 0) -> str:
        lines = [f"{INDENT * depth}{self.label()}"]
        lines += [child.render(depth + 1) for child in self.children()]
        return "\n".join(lines)


# --8<-- [end:component]


def _check_name(name: str) -> None:
    if not name or "/" in name:
        raise ValidationError(f"invalid node name {name!r}")


# --8<-- [start:leaf_and_composite]
@dataclass(slots=True, eq=False)
class File(FileSystemNode):
    """The Leaf: carries the data and has no children. Identity, not value, equality."""

    name: str
    size_bytes: int = 0

    def __post_init__(self) -> None:
        _check_name(self.name)
        if self.size_bytes < 0:
            raise ValidationError("size cannot be negative")

    def size(self) -> int:
        return self.size_bytes

    def children(self) -> Sequence[FileSystemNode]:
        return ()


@dataclass(slots=True, eq=False)
class Directory(FileSystemNode):
    """The Composite: children by name, plus the operations only a container has.

    This is the "safe" form of the pattern: ``add`` and ``remove`` exist on the
    composite alone, so a client holding a ``FileSystemNode`` must resolve a
    ``Directory`` before it can mutate the tree. The transparent form puts them on
    the component and raises on leaves.
    """

    name: str
    _children: dict[str, FileSystemNode] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        _check_name(self.name)

    def size(self) -> int:
        return sum(child.size() for child in self._children.values())

    def children(self) -> Sequence[FileSystemNode]:
        return tuple(self._children.values())

    def label(self) -> str:
        return f"{self.name}/ ({self.size()} B)"

    def add[N: FileSystemNode](self, node: N) -> N:
        if node.name in self._children:
            raise ConflictError(f"{self.name}/ already contains {node.name!r}")
        if any(descendant is self for descendant in node):
            raise ValidationError(f"cannot add {node.name!r} inside its own subtree")
        self._children[node.name] = node
        return node

    def remove(self, name: str) -> FileSystemNode:
        try:
            return self._children.pop(name)
        except KeyError:
            raise NotFoundError(f"{self.name}/ has no entry {name!r}") from None

    def get(self, name: str) -> FileSystemNode:
        try:
            return self._children[name]
        except KeyError:
            raise NotFoundError(f"{self.name}/ has no entry {name!r}") from None

    def resolve(self, path: str) -> FileSystemNode:
        """Walk a slash-separated path such as ``var/log/app.log`` from this directory."""
        node: FileSystemNode = self
        for part in filter(None, path.split("/")):
            if not isinstance(node, Directory):
                raise NotFoundError(f"{node.name!r} is not a directory")
            node = node.get(part)
        return node


# --8<-- [end:leaf_and_composite]


# --8<-- [start:pythonic]
@dataclass(slots=True)
class Node:
    """The single-class form (xml.etree style): a leaf is simply a node with no children."""

    name: str
    size_bytes: int = 0
    children: list[Node] = field(default_factory=list)

    def total(self) -> int:
        return self.size_bytes + sum(child.total() for child in self.children)


def walk(node: Node, depth: int = 0) -> Iterator[tuple[int, Node]]:
    yield depth, node
    for child in node.children:
        yield from walk(child, depth + 1)


def describe(node: FileSystemNode) -> str:
    """An operation kept outside the tree: ``match`` on the node kind (the road to Visitor)."""
    match node:
        case File(name=name, size_bytes=size_bytes):
            return f"file {name}: {size_bytes} B"
        case Directory(name=name):
            return f"dir {name}/: {len(node.children())} entries, {node.size()} B"
        case _:
            raise TypeError(f"unknown node type {type(node).__name__}")


# --8<-- [end:pythonic]


def main() -> None:
    root = Directory("root")
    etc = root.add(Directory("etc"))
    etc.add(File("hosts", 120))
    etc.add(Directory("nginx")).add(File("nginx.conf", 2_048))
    log = root.add(Directory("var")).add(Directory("log"))
    log.add(File("app.log", 1_048_576))
    log.add(File("app.log.1", 524_288))
    root.add(File("README", 512))

    print("--- one render call, leaves and directories alike ---")
    print(root.render())

    print("--- size() is the same question at every level ---")
    for path in ("etc", "etc/nginx", "var/log", "README"):
        print(f"{path}: {root.resolve(path).size()} B")

    print("--- __iter__ turns every whole-tree question into a builtin ---")
    files = list(root.find(lambda node: isinstance(node, File)))
    print(f"nodes: {sum(1 for _ in root)}, files: {len(files)}")
    print(f"files over 100 KB: {[node.name for node in files if node.size() > 100 * 1024]}")
    print(f"largest file: {max(files, key=methodcaller("size")).name}")

    print("--- the composite guards its own invariants ---")
    attempts: list[tuple[str, Callable[[], object]]] = [
        ("duplicate name", lambda: etc.add(File("hosts", 1))),
        ("cycle", lambda: log.add(root)),
        ("missing entry", lambda: etc.remove("passwd")),
    ]
    for label, attempt in attempts:
        try:
            attempt()
        except (ConflictError, ValidationError, NotFoundError) as exc:
            print(f"{label}: {type(exc).__name__}: {exc}")
    removed = log.remove("app.log.1")
    print(f"after removing {removed.name}: var/log is {log.size()} B, root is {root.size()} B")

    print("--- Pythonic variant: one class, a generator walk, match for outside operations ---")
    tree = Node("root", children=[Node("a.txt", 10), Node("docs", children=[Node("b.txt", 20)])])
    print(f"total: {tree.total()} B")
    print(" > ".join(f"{node.name}@{depth}" for depth, node in walk(tree)))
    print(describe(root.resolve("var/log")))
    print(describe(root.resolve("README")))


if __name__ == "__main__":
    main()
