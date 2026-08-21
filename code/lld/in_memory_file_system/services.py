"""The file system operations and the permission proxy that wraps them."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from common import Clock, SystemClock
from lld.in_memory_file_system.models import (
    DirectoryNotEmptyError,
    InvalidPathError,
    IsADirectoryError_,
    NodeStat,
    NotADirectoryError_,
    PathExistsError,
    PathNotFoundError,
    Permission,
    PermissionDeniedError,
    RecursiveMoveError,
    SizeReport,
    User,
)
from lld.in_memory_file_system.nodes import Directory, File, FileHandle, Node
from lld.in_memory_file_system.paths import ROOT, PathResolver
from lld.in_memory_file_system.visitors import NodeVisitor, SearchVisitor, SizeVisitor, TreeVisitor


# --8<-- [start:filesystem]
class FileSystem:
    """The service. It owns path resolution, node creation and the lock ordering.

    There is no global tree lock. Structural changes take the lock of the
    *directory* they mutate; the two operations that touch two directories --
    ``mv`` and ``cp`` -- take both, always sorted by absolute path, so two
    threads swapping files between the same pair cannot deadlock.
    """

    def __init__(self, clock: Clock | None = None, owner: str = "root") -> None:
        self._clock = clock or SystemClock()
        self.root = Directory("", self._clock, owner)
        self._owner = owner

    # --- resolution ----------------------------------------------------------------
    def resolve(self, path: str, cwd: str = ROOT) -> Node:
        node: Node = self.root
        for index, part in enumerate(PathResolver.split(path, cwd)):
            if not node.is_directory():
                raise NotADirectoryError_(f"{'/'.join(PathResolver.split(path, cwd)[:index])} is not a directory")
            child = node.get(part)  # type: ignore[union-attr]
            if child is None:
                raise PathNotFoundError(f"{PathResolver.normalize(path, cwd)} does not exist")
            node = child
        return node

    def exists(self, path: str, cwd: str = ROOT) -> bool:
        try:
            self.resolve(path, cwd)
        except (PathNotFoundError, NotADirectoryError_):
            return False
        return True

    def stat(self, path: str, cwd: str = ROOT) -> NodeStat:
        return self.resolve(path, cwd).stat()

    # --- directories ---------------------------------------------------------------
    def mkdir(self, path: str, parents: bool = True, cwd: str = ROOT) -> Directory:
        """``mkdir -p`` by default: idempotent, and safe when two threads race on it."""
        parts = PathResolver.split(path, cwd)
        if not parts:
            return self.root
        node: Directory = self.root
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            existing = node.get(part)
            if existing is not None:
                if not existing.is_directory():
                    raise NotADirectoryError_(f"{existing.path()} is a file")
                if last and not parents:
                    raise PathExistsError(f"{existing.path()} already exists")
                node = existing  # type: ignore[assignment]
                continue
            if not parents and not last:
                raise PathNotFoundError(f"{node.path() or ROOT} has no child {part}")
            node = node.get_or_add(Directory(part, self._clock, self._owner))  # type: ignore[assignment]
        return node

    def ls(self, path: str = ROOT, cwd: str = ROOT) -> list[str]:
        """LeetCode 588 semantics: a directory lists its sorted children, a file lists itself."""
        node = self.resolve(path, cwd)
        return node.names() if node.is_directory() else [node.name]  # type: ignore[union-attr]

    def walk(self, path: str = ROOT, cwd: str = ROOT) -> Iterator[tuple[str, Node]]:
        """Iterator: depth-first, name-sorted, and a snapshot at each level."""
        node = self.resolve(path, cwd)
        yield node.path() or ROOT, node
        if node.is_directory():
            for child in node.children():  # type: ignore[union-attr]
                yield from self.walk(child.path(), cwd)

    # --- files ----------------------------------------------------------------------
    def create(self, path: str, cwd: str = ROOT) -> File:
        parent, name = self._parent_for(path, cwd, create=True)
        return parent.add(File(name, self._clock, self._owner))  # type: ignore[return-value]

    def write(self, path: str, content: str, cwd: str = ROOT) -> int:
        """Resolve (creating parents), get-or-create the file, replace its content."""
        file = self._file_for_write(path, cwd)
        return file.write(content, self._clock.now())

    def append(self, path: str, content: str, cwd: str = ROOT) -> int:
        file = self._file_for_write(path, cwd)
        return file.append(content, self._clock.now())

    def read(self, path: str, cwd: str = ROOT) -> str:
        node = self.resolve(path, cwd)
        if node.is_directory():
            raise IsADirectoryError_(f"{node.path()} is a directory")
        return node.read()  # type: ignore[union-attr]

    def open(self, path: str, cwd: str = ROOT) -> FileHandle:
        node = self.resolve(path, cwd)
        if node.is_directory():
            raise IsADirectoryError_(f"{node.path()} is a directory")
        return node.open()  # type: ignore[union-attr]

    def _file_for_write(self, path: str, cwd: str) -> File:
        parent, name = self._parent_for(path, cwd, create=True)
        existing = parent.get_or_add(File(name, self._clock, self._owner))
        if existing.is_directory():
            raise IsADirectoryError_(f"{existing.path()} is a directory")
        parent.touch(self._clock.now())
        return existing  # type: ignore[return-value]

    # --- structure ------------------------------------------------------------------
    def rm(self, path: str, recursive: bool = False, cwd: str = ROOT) -> Node:
        target = PathResolver.normalize(path, cwd)
        if target == ROOT:
            raise InvalidPathError("cannot remove the root")
        node = self.resolve(target)
        if node.is_directory() and not node.is_empty() and not recursive:  # type: ignore[union-attr]
            raise DirectoryNotEmptyError(f"{target} is not empty; use recursive=True")
        parent = node.parent
        if parent is None:  # already detached by a concurrent rm
            raise PathNotFoundError(f"{target} is no longer linked")
        with parent.locked():
            parent.remove(node.name)
            parent.modified = self._clock.now()
        self._unlink_subtree(node)
        return node

    def mv(self, source: str, destination: str, cwd: str = ROOT) -> Node:
        """Rename or move. A directory may never be moved into its own subtree."""
        src = PathResolver.normalize(source, cwd)
        dst = PathResolver.normalize(destination, cwd)
        if src == ROOT:
            raise InvalidPathError("cannot move the root")
        node = self.resolve(src)
        if node.is_directory() and PathResolver.is_ancestor(src, dst):
            raise RecursiveMoveError(f"cannot move {src} into its own subtree ({dst})")

        target_parent, target_name = self._destination_for(node, dst)
        old_parent = node.parent
        if old_parent is None:
            raise PathNotFoundError(f"{src} is no longer linked")
        with self._two_locks(old_parent, target_parent):
            if target_parent.get(target_name) is not None:
                raise PathExistsError(f"{PathResolver.join(target_parent.path(), target_name)} already exists")
            old_parent.remove(node.name)
            node.name = target_name
            target_parent.add(node)
            now = self._clock.now()
            old_parent.modified = target_parent.modified = node.modified = now
        return node

    def cp(self, source: str, destination: str, cwd: str = ROOT) -> Node:
        """Deep copy. The clone is a new subtree with fresh timestamps."""
        node = self.resolve(source, cwd)
        dst = PathResolver.normalize(destination, cwd)
        target_parent, target_name = self._destination_for(node, dst)
        clone = self._clone(node, target_name)
        with target_parent.locked():
            target_parent.add(clone)
            target_parent.modified = self._clock.now()
        return clone

    # --- queries ---------------------------------------------------------------------
    def size(self, path: str = ROOT, cwd: str = ROOT) -> int:
        return self.resolve(path, cwd).size()

    def usage(self, path: str = ROOT, cwd: str = ROOT) -> SizeReport:
        visitor = SizeVisitor()
        self.accept(visitor, path, cwd)
        return visitor.report()

    def find(self, path: str = ROOT, name: str | None = None, extension: str | None = None, cwd: str = ROOT) -> list[str]:
        if name is not None:
            visitor = SearchVisitor.by_name(name)
        elif extension is not None:
            visitor = SearchVisitor.by_extension(extension)
        else:
            raise InvalidPathError("find needs a name or an extension")
        self.accept(visitor, path, cwd)
        return visitor.matches

    def tree(self, path: str = ROOT, cwd: str = ROOT) -> str:
        visitor = TreeVisitor()
        self.accept(visitor, path, cwd)
        return visitor.render()

    def accept(self, visitor: NodeVisitor, path: str = ROOT, cwd: str = ROOT) -> None:
        self.resolve(path, cwd).accept(visitor)

    def chmod(self, path: str, owner_permissions: Permission, other_permissions: Permission, cwd: str = ROOT) -> NodeStat:
        node = self.resolve(path, cwd)
        with node.locked():
            node.owner_permissions = owner_permissions
            node.other_permissions = other_permissions
            node.modified = self._clock.now()
        return node.stat()

    # --- internals --------------------------------------------------------------------
    def _parent_for(self, path: str, cwd: str, create: bool) -> tuple[Directory, str]:
        parts = PathResolver.split(path, cwd)
        if not parts:
            raise InvalidPathError("a file needs a name")
        parent_path = PathResolver.join(*parts[:-1]) if parts[:-1] else ROOT
        parent = self.mkdir(parent_path) if create else self.resolve(parent_path)
        if not parent.is_directory():
            raise NotADirectoryError_(f"{parent_path} is not a directory")
        return parent, parts[-1]  # type: ignore[return-value]

    def _destination_for(self, node: Node, dst: str) -> tuple[Directory, str]:
        """``mv a b/`` where b exists means "into b"; otherwise b is the new name."""
        if self.exists(dst):
            existing = self.resolve(dst)
            if existing.is_directory():
                return existing, node.name  # type: ignore[return-value]
            raise PathExistsError(f"{dst} already exists")
        parent, name = self._parent_for(dst, ROOT, create=False)
        return parent, name

    def _clone(self, node: Node, name: str) -> Node:
        if not node.is_directory():
            clone = File(name, self._clock, node.owner)
            clone.write(node.read(), self._clock.now())  # type: ignore[union-attr]
            return clone
        directory = Directory(name, self._clock, node.owner)
        for child in node.children():  # type: ignore[union-attr]
            directory.add(self._clone(child, child.name))
        return directory

    def _unlink_subtree(self, node: Node) -> None:
        if node.is_directory():
            for child in node.children():  # type: ignore[union-attr]
                self._unlink_subtree(child)
        node.unlink()  # type: ignore[union-attr]

    @contextmanager
    def _two_locks(self, first: Node, second: Node) -> Iterator[None]:
        """Always by absolute path: one global order means no lock cycle."""
        if first is second:
            with first.locked():
                yield
            return
        low, high = sorted((first, second), key=lambda node: node.path())
        with low.locked(), high.locked():
            yield


# --8<-- [end:filesystem]


# --8<-- [start:proxy]
class SecureFileSystem:
    """Proxy: the same surface as ``FileSystem``, with a permission check first.

    Putting the checks in a proxy rather than inside every method keeps the core
    readable and makes "run this batch job as root" a one-line change of user.
    """

    def __init__(self, filesystem: FileSystem, user: User) -> None:
        self._fs = filesystem
        self.user = user

    def read(self, path: str) -> str:
        self._require(path, Permission.READ)
        return self._fs.read(path)

    def write(self, path: str, content: str) -> int:
        self._require_parent(path, Permission.WRITE)
        if self._fs.exists(path):
            self._require(path, Permission.WRITE)
        return self._fs.write(path, content)

    def ls(self, path: str = ROOT) -> list[str]:
        self._require(path, Permission.READ | Permission.EXECUTE)
        return self._fs.ls(path)

    def mkdir(self, path: str) -> Directory:
        self._require_parent(path, Permission.WRITE)
        return self._fs.mkdir(path)

    def rm(self, path: str, recursive: bool = False) -> Node:
        self._require_parent(path, Permission.WRITE)
        return self._fs.rm(path, recursive)

    def stat(self, path: str) -> NodeStat:
        self._require(path, Permission.READ)
        return self._fs.stat(path)

    def _require(self, path: str, needed: Permission) -> Node:
        node = self._fs.resolve(path)
        granted = node.effective_permissions(self.user.name, self.user.is_admin)
        if not granted.allows(needed):
            raise PermissionDeniedError(f"{self.user.name} lacks {needed.name} on {node.path() or ROOT}")
        return node

    def _require_parent(self, path: str, needed: Permission) -> None:
        """Check the nearest ancestor that *exists*, not just the immediate parent.

        Stopping at a missing parent is a hole, because ``mkdir`` creates every
        level: ``mkdir /private/deep/sub`` under a root-only ``/private`` would
        find no ``/private/deep`` to check, run no check at all, and create the
        chain anyway. The root always exists, so the walk terminates.
        """
        current = PathResolver.parent(path)
        while current != ROOT and not self._fs.exists(current):
            current = PathResolver.parent(current)
        self._require(current, needed)


# --8<-- [end:proxy]
