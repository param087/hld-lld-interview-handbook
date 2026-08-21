"""Path arithmetic. Every string edge case an interviewer probes lives in one class."""

from __future__ import annotations

from lld.in_memory_file_system.models import InvalidPathError, validate_name

SEPARATOR = "/"
ROOT = "/"


# --8<-- [start:resolver]
class PathResolver:
    """Pure string handling -- no tree, no locks, therefore trivially testable.

    Keeping resolution out of ``FileSystem`` is deliberate: the tricky cases are
    ``//`` runs, a trailing slash, ``.``, ``..`` past the root, and relative
    paths against a working directory. All of them are decided here, once.
    """

    @staticmethod
    def normalize(path: str, cwd: str = ROOT) -> str:
        """Absolute, separator-collapsed, dot-free. ``..`` at the root stays at the root."""
        if not path or not path.strip():
            raise InvalidPathError("path must be a non-empty string")
        raw = path if path.startswith(SEPARATOR) else f"{cwd.rstrip(SEPARATOR)}{SEPARATOR}{path}"
        parts: list[str] = []
        for part in raw.split(SEPARATOR):
            if part in ("", "."):
                continue  # collapses "//" and a trailing "/"
            if part == "..":
                if parts:
                    parts.pop()  # "/.." is "/", never an error
                continue
            parts.append(validate_name(part))
        return ROOT + SEPARATOR.join(parts)

    @staticmethod
    def split(path: str, cwd: str = ROOT) -> list[str]:
        normalized = PathResolver.normalize(path, cwd)
        return [] if normalized == ROOT else normalized.lstrip(SEPARATOR).split(SEPARATOR)

    @staticmethod
    def parent(path: str, cwd: str = ROOT) -> str:
        parts = PathResolver.split(path, cwd)
        return ROOT + SEPARATOR.join(parts[:-1])

    @staticmethod
    def basename(path: str, cwd: str = ROOT) -> str:
        parts = PathResolver.split(path, cwd)
        return parts[-1] if parts else ""

    @staticmethod
    def join(*parts: str) -> str:
        joined = SEPARATOR.join(part.strip(SEPARATOR) for part in parts if part.strip(SEPARATOR))
        return ROOT + joined

    @staticmethod
    def is_ancestor(ancestor: str, descendant: str) -> bool:
        """``/a`` is an ancestor of ``/a/b`` but not of ``/ab`` -- the check mv needs."""
        if ancestor == ROOT:
            return True
        return descendant == ancestor or descendant.startswith(ancestor + SEPARATOR)


# --8<-- [end:resolver]
