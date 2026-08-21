"""Shared pytest configuration for every handbook package.

The only thing here is a guard against a failure mode a reviewer found across
the whole repo: at CPython's default switch interval (5 ms) a thread usually
runs a short critical section to completion, so a concurrency test can pass
even when the lock it is supposed to prove has been deleted. That makes the
test decorative.

Any test whose name mentions concurrency runs with a 1 us switch interval, so
the interpreter preempts inside short critical sections and a missing lock
actually loses. The interval is restored afterwards.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

# Tests naming any of these are treated as concurrency tests.
_CONCURRENCY_HINTS = (
    "concurrent",
    "concurrency",
    "thread",
    "race",
    "parallel",
    "simultaneous",
    "at_the_same_time",
    "atomic",
    "lock",
    "deadlock",
    "contention",
)

_TIGHT_INTERVAL = 1e-6


@pytest.fixture(autouse=True)
def _preempt_aggressively_in_concurrency_tests(request: pytest.FixtureRequest) -> Iterator[None]:
    """Shorten the GIL switch interval for concurrency tests so races are reachable."""
    name = request.node.name.lower()
    if not any(hint in name for hint in _CONCURRENCY_HINTS):
        yield
        return
    previous = sys.getswitchinterval()
    sys.setswitchinterval(_TIGHT_INTERVAL)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)
