"""Permissions, statuses, stat records and the domain errors.

The tree itself lives in ``nodes.py``; this module is only vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag, StrEnum

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError

MAX_NAME_LENGTH = 255
RESERVED_NAMES = ("", ".", "..")


# --8<-- [start:enums]
class NodeStatus(StrEnum):
    """POSIX unlink semantics in three words.

    Removing a file takes it out of the directory tree, but the bytes survive
    while a handle is still open -- which is why ``rm`` on a log a process is
    writing does not free the disk until the process restarts.
    """

    ACTIVE = "active"  # reachable from the root
    UNLINKED = "unlinked"  # removed from its parent, still held by an open handle
    RELEASED = "released"  # last handle closed, storage reclaimed


class Permission(IntFlag):
    """The classic three bits. ``IntFlag`` so ``READ | WRITE`` is one value."""

    NONE = 0
    EXECUTE = 1  # on a directory: may traverse into it
    WRITE = 2
    READ = 4
    READ_WRITE = READ | WRITE
    ALL = READ | WRITE | EXECUTE

    def allows(self, needed: Permission) -> bool:
        return (self & needed) == needed


# --8<-- [end:enums]


# --8<-- [start:errors]
class PathNotFoundError(NotFoundError):
    """No node at that path."""


class PathExistsError(ConflictError):
    """A node already exists where one was to be created."""


class NotADirectoryError_(ValidationError):
    """A path component that must be a directory is a file."""


class IsADirectoryError_(ValidationError):
    """A directory was passed where a file was required (or rm without -r)."""


class InvalidPathError(ValidationError):
    """Empty path, empty component, or a name that is too long."""


class DirectoryNotEmptyError(ConflictError):
    """rm on a non-empty directory without the recursive flag."""


class RecursiveMoveError(ConflictError):
    """mv of a directory into its own subtree."""


class PermissionDeniedError(ConflictError):
    """The user lacks a required bit on the node or on a parent directory."""


class NodeReleasedError(InvalidStateError):
    """Operation on a handle whose file has already been released."""


# --8<-- [end:errors]


# --8<-- [start:values]
@dataclass(frozen=True, slots=True)
class User:
    """Identity for the permission proxy. ``is_admin`` is the root bypass."""

    name: str
    is_admin: bool = False


@dataclass(frozen=True, slots=True)
class NodeStat:
    """What ``stat`` returns: everything a caller needs without touching the node."""

    path: str
    is_directory: bool
    size: int
    owner: str
    status: NodeStatus
    created: float
    modified: float
    owner_permissions: Permission
    other_permissions: Permission

    def mode(self) -> str:
        def bits(permission: Permission) -> str:
            return "".join(
                flag if permission.allows(value) else "-"
                for flag, value in (("r", Permission.READ), ("w", Permission.WRITE), ("x", Permission.EXECUTE))
            )

        return ("d" if self.is_directory else "-") + bits(self.owner_permissions) + bits(self.other_permissions)


@dataclass(frozen=True, slots=True)
class SizeReport:
    """What ``SizeVisitor`` accumulates: one traversal, four answers."""

    files: int
    directories: int
    total_bytes: int
    largest_file: str | None


# --8<-- [end:values]


def validate_name(name: str) -> str:
    """Names, not paths: no separators, no reserved words, bounded length."""
    if name in RESERVED_NAMES or "/" in name or len(name) > MAX_NAME_LENGTH:
        raise InvalidPathError(f"invalid node name: {name!r}")
    return name
