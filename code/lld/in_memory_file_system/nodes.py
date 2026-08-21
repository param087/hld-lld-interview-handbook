"""The Composite tree: Node, File, Directory and the handle that keeps bytes alive."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Protocol

from common import Clock
from lld.in_memory_file_system.models import (
    NodeReleasedError,
    NodeStat,
    NodeStatus,
    PathExistsError,
    PathNotFoundError,
    Permission,
    validate_name,
)

if TYPE_CHECKING:
    from lld.in_memory_file_system.visitors import NodeVisitor


# --8<-- [start:node]
class Node(ABC):
    """Composite base: a file and a directory answer the same questions.

    Every node owns an ``RLock``. A directory's lock guards its children dict;
    a file's lock guards its content. Nothing guards the whole tree, so two
    threads working in different directories never meet.
    """

    def __init__(
        self,
        name: str,
        clock: Clock,
        owner: str = "root",
        owner_permissions: Permission = Permission.ALL,
        other_permissions: Permission = Permission.READ | Permission.EXECUTE,
    ) -> None:
        self.name = validate_name(name) if name else name  # only the root is nameless
        self.owner = owner
        self.owner_permissions = owner_permissions
        self.other_permissions = other_permissions
        self.parent: Directory | None = None
        self.status = NodeStatus.ACTIVE
        self.created = clock.now()
        self.modified = self.created
        self._lock = threading.RLock()

    @abstractmethod
    def size(self) -> int:
        """Composite: a file knows its bytes, a directory sums its children."""

    @abstractmethod
    def accept(self, visitor: NodeVisitor) -> None:
        """Visitor: double dispatch, so new traversals need no change here."""

    @abstractmethod
    def is_directory(self) -> bool: ...

    def path(self) -> str:
        parts: list[str] = []
        node: Node | None = self
        while node is not None and node.parent is not None:
            parts.append(node.name)
            node = node.parent
        return "/" + "/".join(reversed(parts))

    def touch(self, now: float) -> None:
        with self._lock:
            self.modified = now

    def effective_permissions(self, user_name: str, is_admin: bool = False) -> Permission:
        if is_admin or user_name == self.owner:
            return self.owner_permissions
        return self.other_permissions

    def stat(self) -> NodeStat:
        with self._lock:
            return NodeStat(
                path=self.path(),
                is_directory=self.is_directory(),
                size=self.size(),
                owner=self.owner,
                status=self.status,
                created=self.created,
                modified=self.modified,
                owner_permissions=self.owner_permissions,
                other_permissions=self.other_permissions,
            )

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.path()!r})"


# --8<-- [end:node]


# --8<-- [start:file]
class File(Node):
    """A leaf. Content is a list of chunks so append is O(1), not O(size)."""

    def __init__(self, name: str, clock: Clock, owner: str = "root") -> None:
        super().__init__(name, clock, owner, Permission.READ_WRITE, Permission.READ)
        self._chunks: list[str] = []
        self._size = 0
        self._handles = 0

    def is_directory(self) -> bool:
        return False

    def size(self) -> int:
        with self._lock:
            return self._size

    def read(self) -> str:
        with self._lock:
            self._require_readable()
            return "".join(self._chunks)

    def write(self, content: str, now: float) -> int:
        with self._lock:
            self._require_readable()
            self._chunks = [content]
            self._size = len(content)
            self.modified = now
            return self._size

    def append(self, content: str, now: float) -> int:
        with self._lock:
            self._require_readable()
            self._chunks.append(content)
            self._size += len(content)
            self.modified = now
            return self._size

    def accept(self, visitor: NodeVisitor) -> None:
        visitor.visit_file(self)

    def open(self) -> FileHandle:
        """An open handle keeps the bytes alive after ``rm`` -- POSIX unlink semantics."""
        with self._lock:
            self._require_readable()
            self._handles += 1
            return FileHandle(self)

    def close_handle(self) -> None:
        with self._lock:
            self._handles = max(0, self._handles - 1)
            if self._handles == 0 and self.status is NodeStatus.UNLINKED:
                self._release_locked()

    def unlink(self) -> NodeStatus:
        """Removed from the tree. Released now if nobody has it open."""
        with self._lock:
            self.status = NodeStatus.UNLINKED
            if self._handles == 0:
                self._release_locked()
            return self.status

    def open_handles(self) -> int:
        with self._lock:
            return self._handles

    def _release_locked(self) -> None:
        self._chunks = []
        self._size = 0
        self.status = NodeStatus.RELEASED

    def _require_readable(self) -> None:
        if self.status is NodeStatus.RELEASED:
            raise NodeReleasedError(f"{self.name} has been released")


class FileHandle:
    """Proof that the file outlives its directory entry while someone holds it."""

    def __init__(self, file: File) -> None:
        self.file = file
        self._closed = False

    def read(self) -> str:
        return self.file.read()

    def append(self, content: str, now: float) -> int:
        return self.file.append(content, now)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.file.close_handle()

    def __enter__(self) -> FileHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --8<-- [end:file]


# --8<-- [start:directory]
class Directory(Node):
    """A composite. Its lock guards the children dict and nothing else."""

    def __init__(self, name: str, clock: Clock, owner: str = "root") -> None:
        super().__init__(name, clock, owner)
        self._children: dict[str, Node] = {}

    def is_directory(self) -> bool:
        return True

    def size(self) -> int:
        with self._lock:
            children = list(self._children.values())
        return sum(child.size() for child in children)  # recursion outside our own lock

    def accept(self, visitor: NodeVisitor) -> None:
        visitor.visit_directory(self)

    def add(self, node: Node) -> Node:
        with self._lock:
            if node.name in self._children:
                raise PathExistsError(f"{node.name} already exists in {self.path() or '/'}")
            self._children[node.name] = node
            node.parent = self
            return node

    def get_or_add(self, node: Node) -> Node:
        """mkdir -p: idempotent under contention, because it decides under the lock."""
        with self._lock:
            existing = self._children.get(node.name)
            if existing is not None:
                return existing
            self._children[node.name] = node
            node.parent = self
            return node

    def remove(self, name: str) -> Node:
        with self._lock:
            node = self._children.pop(name, None)
            if node is None:
                raise PathNotFoundError(f"{name} not found in {self.path() or '/'}")
            node.parent = None
            return node

    def get(self, name: str) -> Node | None:
        with self._lock:
            return self._children.get(name)

    def children(self) -> list[Node]:
        with self._lock:
            return [self._children[name] for name in sorted(self._children)]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._children)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._children

    def unlink(self) -> NodeStatus:
        with self._lock:
            self.status = NodeStatus.RELEASED
            return self.status


# --8<-- [end:directory]


class NodeFactory(Protocol):
    """Factory seam: a symlink or a device node would be a third implementation."""

    def create(self, name: str, clock: Clock, owner: str) -> Node: ...
