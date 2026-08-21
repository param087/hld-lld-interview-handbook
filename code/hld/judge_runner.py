"""An online-judge submission queue and a simulated sandboxed runner with limits and verdicts.

The crux of the LeetCode design in one module:

* :class:`SubmissionQueue` is a lease queue: a worker claims a submission, and if the worker
  dies the lease expires and another worker picks it up. Exhausted attempts become a verdict
  of ``INTERNAL_ERROR`` instead of a submission that hangs forever in "judging".
* :class:`SimulatedSandbox` stands in for a container with a seccomp profile and cgroup limits:
  it enforces a wall-clock limit, a memory limit and an output limit, and reports what the
  program actually consumed.
* :class:`Judge` runs the test cases in order, streams a result per case, and stops at the
  first failure -- with a documented **verdict precedence**, because a process killed by the
  out-of-memory killer also exits non-zero and would otherwise look like a runtime error.

Nothing here reads a clock directly: time is injected, so the demo and the tests are exact.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from common import Clock, NotFoundError, SystemClock, ValidationError


# --8<-- [start:verdicts]
class Verdict(StrEnum):
    """The taxonomy a user sees. Order matters: see ``Judge._verdict_for``."""

    ACCEPTED = "Accepted"
    WRONG_ANSWER = "Wrong Answer"
    TIME_LIMIT_EXCEEDED = "Time Limit Exceeded"
    MEMORY_LIMIT_EXCEEDED = "Memory Limit Exceeded"
    RUNTIME_ERROR = "Runtime Error"
    COMPILE_ERROR = "Compile Error"
    OUTPUT_LIMIT_EXCEEDED = "Output Limit Exceeded"
    INTERNAL_ERROR = "Internal Error"  # ours, not the user's: retried before it is ever shown


@dataclass(frozen=True, slots=True)
class Limits:
    """Per-language limits. The runtime multiplier lives in the problem's language table."""

    time_ms: int = 1_000
    memory_kb: int = 256 * 1024
    output_kb: int = 64


@dataclass(frozen=True, slots=True)
class ProblemCase:
    case_id: str
    stdin: str
    expected_stdout: str
    is_sample: bool = False


@dataclass(frozen=True, slots=True)
class RunResult:
    """What the sandbox reports back for one execution."""

    exit_code: int
    stdout: str
    time_ms: int
    memory_kb: int
    output_kb: int


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    verdict: Verdict
    time_ms: int
    memory_kb: int


@dataclass(frozen=True, slots=True)
class Judgement:
    """The final, deterministic answer for one submission."""

    verdict: Verdict
    cases_passed: int
    total_cases: int
    failed_case_id: str | None = None
    max_time_ms: int = 0
    max_memory_kb: int = 0
    detail: str | None = None


# --8<-- [end:verdicts]


# --8<-- [start:sandbox]
@dataclass(frozen=True, slots=True)
class Program:
    """A stand-in for a compiled binary: what it prints and what it costs on each input."""

    outputs: Mapping[str, str] = field(default_factory=dict)
    time_ms: int = 1
    memory_kb: int = 1_024
    crashes_on: frozenset[str] = frozenset()
    compile_error: str | None = None
    default_stdout: str = ""


class Sandbox(Protocol):
    def run(self, program: Program, stdin: str, limits: Limits) -> RunResult: ...


class SimulatedSandbox:
    """A container with no network, a read-only root, a seccomp profile and cgroup limits.

    The real thing kills the process; here we compute the same outcome deterministically.
    Limits are enforced *before* the output is trusted, because a program killed at the limit
    has produced partial output that must never be compared against the expected answer.
    """

    def run(self, program: Program, stdin: str, limits: Limits) -> RunResult:
        if program.memory_kb > limits.memory_kb:
            # The cgroup out-of-memory killer stops the process: no usable output, non-zero exit.
            return RunResult(137, "", min(program.time_ms, limits.time_ms), program.memory_kb, 0)
        if program.time_ms > limits.time_ms:
            # The watchdog sends SIGKILL at the wall-clock limit.
            return RunResult(137, "", limits.time_ms, program.memory_kb, 0)
        if stdin in program.crashes_on:
            return RunResult(1, "", program.time_ms, program.memory_kb, 0)
        stdout = program.outputs.get(stdin, program.default_stdout)
        output_kb = len(stdout.encode()) // 1024
        if output_kb > limits.output_kb:
            return RunResult(0, stdout[: limits.output_kb * 1024], program.time_ms,
                             program.memory_kb, output_kb)
        return RunResult(0, stdout, program.time_ms, program.memory_kb, output_kb)


# --8<-- [end:sandbox]


# --8<-- [start:judge]
class Checker(Protocol):
    def matches(self, expected: str, actual: str) -> bool: ...


class TokenChecker:
    """Default checker: whitespace-insensitive token comparison.

    Judging on raw bytes fails half the correct submissions on a trailing newline, so
    normalise; problems that accept several answers ship their own special judge instead.
    """

    def matches(self, expected: str, actual: str) -> bool:
        return expected.split() == actual.split()


class Judge:
    """Runs one submission against the test cases and turns runs into a single verdict."""

    def __init__(self, sandbox: Sandbox | None = None, checker: Checker | None = None) -> None:
        self._sandbox = sandbox or SimulatedSandbox()
        self._checker = checker or TokenChecker()

    def judge(
        self,
        program: Program,
        cases: Sequence[ProblemCase],
        limits: Limits | None = None,
        on_case: Callable[[CaseResult], None] | None = None,
    ) -> Judgement:
        if not cases:
            raise ValidationError("a problem needs at least one test case")
        limits = limits or Limits()
        if program.compile_error is not None:
            return Judgement(Verdict.COMPILE_ERROR, 0, len(cases), detail=program.compile_error)
        passed = max_time = max_memory = 0
        for case in cases:
            run = self._sandbox.run(program, case.stdin, limits)
            verdict = self._verdict_for(run, case, limits)
            max_time = max(max_time, run.time_ms)
            max_memory = max(max_memory, run.memory_kb)
            if on_case is not None:
                on_case(CaseResult(case.case_id, verdict, run.time_ms, run.memory_kb))
            if verdict is not Verdict.ACCEPTED:
                # Stop at the first failure: the verdict is decided and the worker is freed.
                return Judgement(verdict, passed, len(cases), case.case_id, max_time, max_memory)
            passed += 1
        return Judgement(Verdict.ACCEPTED, passed, len(cases), None, max_time, max_memory)

    def _verdict_for(self, run: RunResult, case: ProblemCase, limits: Limits) -> Verdict:
        """Precedence is the whole game: the resource limits are checked before the exit code,
        because a process killed by the out-of-memory killer or the watchdog also exits non-zero.
        """
        if run.memory_kb > limits.memory_kb:
            return Verdict.MEMORY_LIMIT_EXCEEDED
        if run.time_ms >= limits.time_ms:
            return Verdict.TIME_LIMIT_EXCEEDED
        if run.output_kb > limits.output_kb:
            return Verdict.OUTPUT_LIMIT_EXCEEDED
        if run.exit_code != 0:
            return Verdict.RUNTIME_ERROR
        if not self._checker.matches(case.expected_stdout, run.stdout):
            return Verdict.WRONG_ANSWER
        return Verdict.ACCEPTED


# --8<-- [end:judge]


# --8<-- [start:queue]
@dataclass(slots=True)
class _Lease:
    submission_id: str
    expires_at: float
    attempt: int


class SubmissionQueue:
    """FIFO queue with leases, so a worker that dies does not lose the submission.

    ``_lock`` guards ``_ready``, ``_leases`` and ``_attempts`` together; every public method
    takes it, which is why hundreds of workers can claim concurrently without double-judging.
    """

    def __init__(
        self,
        clock: Clock | None = None,
        lease_seconds: float = 30.0,
        max_attempts: int = 3,
    ) -> None:
        self._clock = clock or SystemClock()
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._ready: list[str] = []
        self._leases: dict[str, _Lease] = {}
        self._attempts: dict[str, int] = {}
        self._dead: list[str] = []
        self._lock = threading.Lock()

    def submit(self, submission_id: str) -> None:
        with self._lock:
            if submission_id in self._attempts:
                raise ValidationError(f"submission {submission_id!r} was already queued")
            self._attempts[submission_id] = 0
            self._ready.append(submission_id)

    def claim(self) -> str | None:
        """Take the next submission and lease it. Returns None when the queue is empty."""
        with self._lock:
            if not self._ready:
                return None
            submission_id = self._ready.pop(0)
            self._attempts[submission_id] += 1
            self._leases[submission_id] = _Lease(
                submission_id,
                self._clock.now() + self._lease_seconds,
                self._attempts[submission_id],
            )
            return submission_id

    def complete(self, submission_id: str) -> None:
        with self._lock:
            if self._leases.pop(submission_id, None) is None:
                raise NotFoundError(f"submission {submission_id!r} is not leased")

    def reclaim_expired(self) -> list[str]:
        """Re-queue leases that outlived a crashed worker; give up after ``max_attempts``."""
        now = self._clock.now()
        with self._lock:
            expired = [lease for lease in self._leases.values() if lease.expires_at <= now]
            for lease in expired:
                del self._leases[lease.submission_id]
                if lease.attempt >= self._max_attempts:
                    self._dead.append(lease.submission_id)
                else:
                    self._ready.append(lease.submission_id)
            return [lease.submission_id for lease in expired]

    def dead_letters(self) -> list[str]:
        """Submissions that failed every attempt: they get INTERNAL_ERROR and a page."""
        with self._lock:
            return list(self._dead)

    def depth(self) -> tuple[int, int]:
        """(ready, in flight) -- the two numbers a judging queue is autoscaled on."""
        with self._lock:
            return len(self._ready), len(self._leases)


# --8<-- [end:queue]


def main() -> None:
    from common import FakeClock

    cases = [
        ProblemCase("t1", "2 3", "5", is_sample=True),
        ProblemCase("t2", "10 20", "30"),
        ProblemCase("t3", "big", "1000000"),
    ]
    submissions = {
        "sub-correct": Program({"2 3": "5\n", "10 20": "30", "big": "1000000"}),
        "sub-wrong": Program({"2 3": "5", "10 20": "31"}),
        "sub-slow": Program({"2 3": "5"}, time_ms=4_000),
        "sub-hungry": Program({"2 3": "5"}, memory_kb=512 * 1024),
        "sub-crash": Program({"2 3": "5"}, crashes_on=frozenset({"10 20"})),
        "sub-broken": Program(compile_error="line 7: expected ';'"),
    }
    judge = Judge()
    limits = Limits(time_ms=2_000, memory_kb=256 * 1024)

    def judge_streaming(program: Program) -> tuple[Judgement, list[str]]:
        """Collect what a websocket would have pushed to the browser, case by case."""
        streamed: list[str] = []
        result = judge.judge(program, cases, limits, on_case=lambda r: streamed.append(r.case_id))
        return result, streamed

    for name, program in submissions.items():
        result, streamed = judge_streaming(program)
        failed = f" at {result.failed_case_id}" if result.failed_case_id else ""
        print(
            f"{name:<12} {result.verdict.value:<21} {result.cases_passed}/{result.total_cases}"
            f"{failed}  streamed={streamed}"
        )

    clock = FakeClock(start=1_000.0)
    queue = SubmissionQueue(clock=clock, lease_seconds=30.0, max_attempts=2)
    for name in ("sub-correct", "sub-wrong"):
        queue.submit(name)
    claimed = queue.claim()
    print(f"worker A claimed {claimed}, queue depth (ready, in flight) = {queue.depth()}")
    clock.advance(31)  # worker A was killed by the node autoscaler
    print(f"reclaimed after the lease expired: {queue.reclaim_expired()}")
    again = queue.claim()
    if again is not None:
        queue.complete(again)
    print(f"worker B judged {again}; depth now {queue.depth()}, dead letters {queue.dead_letters()}")


if __name__ == "__main__":
    main()
