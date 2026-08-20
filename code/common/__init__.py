"""Shared, frozen utilities used by every HLD/LLD artifact in the handbook.

Authors import from here; nobody edits these after wave 1.
"""

from common.clock import Clock, FakeClock, SystemClock
from common.errors import (
    ConflictError,
    HandbookError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from common.ids import IdGenerator, SequentialIdGenerator, UuidIdGenerator
from common.money import Money

__all__ = [
    "Clock",
    "ConflictError",
    "FakeClock",
    "HandbookError",
    "IdGenerator",
    "InvalidStateError",
    "Money",
    "NotFoundError",
    "SequentialIdGenerator",
    "SystemClock",
    "UuidIdGenerator",
    "ValidationError",
]
