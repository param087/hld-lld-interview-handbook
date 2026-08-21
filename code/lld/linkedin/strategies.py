"""Job search Specifications and feed ranking — the two rules that vary.

The Specification algebra is the same shape you would use for any filterable
collection: leaves answer one question, ``&``, ``|`` and ``~`` build a tree, and
``JobService`` never grows a parameter per filter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Protocol

from lld.linkedin.models import Job, Post


# --8<-- [start:specification]
class JobSpec(ABC):
    """One yes/no question about a job, composable with ``&``, ``|`` and ``~``."""

    @abstractmethod
    def is_satisfied_by(self, job: Job) -> bool: ...

    @abstractmethod
    def describe(self) -> str: ...

    def __and__(self, other: JobSpec) -> JobSpec:
        return AndSpec(self, other)

    def __or__(self, other: JobSpec) -> JobSpec:
        return OrSpec(self, other)

    def __invert__(self) -> JobSpec:
        return NotSpec(self)


class AndSpec(JobSpec):
    def __init__(self, left: JobSpec, right: JobSpec) -> None:
        self._left, self._right = left, right

    def is_satisfied_by(self, job: Job) -> bool:
        return self._left.is_satisfied_by(job) and self._right.is_satisfied_by(job)

    def describe(self) -> str:
        return f"({self._left.describe()} AND {self._right.describe()})"


class OrSpec(JobSpec):
    def __init__(self, left: JobSpec, right: JobSpec) -> None:
        self._left, self._right = left, right

    def is_satisfied_by(self, job: Job) -> bool:
        return self._left.is_satisfied_by(job) or self._right.is_satisfied_by(job)

    def describe(self) -> str:
        return f"({self._left.describe()} OR {self._right.describe()})"


class NotSpec(JobSpec):
    def __init__(self, inner: JobSpec) -> None:
        self._inner = inner

    def is_satisfied_by(self, job: Job) -> bool:
        return not self._inner.is_satisfied_by(job)

    def describe(self) -> str:
        return f"NOT {self._inner.describe()}"


class InLocation(JobSpec):
    def __init__(self, location: str) -> None:
        self._location = location.strip().lower()

    def is_satisfied_by(self, job: Job) -> bool:
        return job.location.lower() == self._location

    def describe(self) -> str:
        return f"location={self._location}"


class RemoteOnly(JobSpec):
    def is_satisfied_by(self, job: Job) -> bool:
        return job.remote

    def describe(self) -> str:
        return "remote=yes"


class RequiresSkill(JobSpec):
    def __init__(self, skill: str) -> None:
        self._skill = skill.strip().lower()

    def is_satisfied_by(self, job: Job) -> bool:
        return self._skill in job.skills

    def describe(self) -> str:
        return f"skill={self._skill}"


class MaxExperience(JobSpec):
    """Jobs a member with this much experience actually qualifies for."""

    def __init__(self, years: int) -> None:
        self._years = years

    def is_satisfied_by(self, job: Job) -> bool:
        return job.min_experience <= self._years

    def describe(self) -> str:
        return f"min_experience<={self._years}"


class AtCompany(JobSpec):
    def __init__(self, company_id: str) -> None:
        self._company_id = company_id

    def is_satisfied_by(self, job: Job) -> bool:
        return job.company_id == self._company_id

    def describe(self) -> str:
        return f"company={self._company_id}"


# --8<-- [end:specification]


# --8<-- [start:ranking]
class FeedRanking(Protocol):
    """Orders the posts a viewer is allowed to see."""

    def rank(self, posts: Sequence[Post]) -> list[Post]: ...


class ChronologicalFeed:
    """Newest first. What members expect, and what you should default to."""

    def rank(self, posts: Sequence[Post]) -> list[Post]:
        return sorted(posts, key=lambda p: -p.created_at)


class EngagementFeed:
    """Reactions and comments first, recency as the tiebreak."""

    def rank(self, posts: Sequence[Post]) -> list[Post]:
        return sorted(posts, key=lambda p: (-p.engagement(), -p.created_at))


class DegreeWeightedFeed:
    """Closer connections first: the ranking that needs the graph, injected as a map."""

    def __init__(self, degrees: dict[str, int]) -> None:
        self._degrees = degrees

    def rank(self, posts: Sequence[Post]) -> list[Post]:
        return sorted(posts, key=lambda p: (self._degrees.get(p.author_id, 9), -p.created_at))


# --8<-- [end:ranking]
