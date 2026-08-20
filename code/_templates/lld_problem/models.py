"""T: Enums, value objects (frozen dataclasses), entities and domain exceptions.

T: Keep this file free of business logic beyond simple invariants/validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from common import HandbookError


class ExampleStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class ExampleError(HandbookError):
    """T: Domain-specific errors subclass HandbookError (or its children)."""


@dataclass(frozen=True, slots=True)
class ExampleValue:
    amount: int


@dataclass(slots=True)
class ExampleEntity:
    id: str
    status: ExampleStatus = ExampleStatus.ACTIVE
    tags: list[str] = field(default_factory=list)
