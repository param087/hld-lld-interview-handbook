"""Thread-safe circuit breaker: closed, open and half-open, driven by an injected clock.

What the module demonstrates, in the order an interviewer asks about it:

* ``CircuitBreaker.call`` guards one call to a dependency. While CLOSED every call passes and
  its outcome lands in a sliding window; once the window holds ``min_calls`` outcomes and the
  failure rate reaches ``failure_rate_threshold`` the breaker OPENS and rejects every call at
  once with ``CircuitOpenError`` (which carries ``retry_after``), so callers fail in
  microseconds instead of holding a thread for a whole timeout, and the dependency gets room
  to recover.
* After ``open_seconds`` the breaker turns HALF_OPEN and admits ``half_open_max_calls`` trial
  calls at a time: ``success_threshold`` successes close it, one failure reopens it.
* ``acquire`` / ``record`` / ``release`` expose the same machine to code that cannot wrap a
  callable (async handlers, streams). A ``Permit`` ties each outcome to the state episode that
  issued it, so a late result from before a reset or a reopen cannot corrupt the current one.
* ``ignored_exceptions`` (caller bugs such as a validation error) pass through uncounted, and
  a call slower than ``slow_call_seconds`` counts as a failure even though it returned, because
  a dependency that answers late is the one that causes cascading failures.

``_lock`` guards every field of a breaker; the dependency is always called outside it.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from common import Clock, FakeClock, InvalidStateError, SystemClock, ValidationError


# --8<-- [start:policy]
class State(StrEnum):
    CLOSED = "closed"  # calls pass; outcomes are counted
    OPEN = "open"  # calls are rejected at once
    HALF_OPEN = "half_open"  # a bounded number of trial calls probe the dependency


@dataclass(frozen=True, slots=True)
class BreakerPolicy:
    """What opens the breaker, how long it stays open and what closes it again.

    Trip on a *failure rate over a window with a minimum volume*, not on N consecutive
    failures: at ~1k QPS five consecutive failures are 5 ms of a blip, while at 1 QPS a rate
    without a minimum volume opens the breaker on the first failure of the day.
    """

    failure_rate_threshold: float = 0.5  # share of failed calls in the window that opens it
    min_calls: int = 10  # outcomes the window must hold before the rate is judged
    window_seconds: float = 10.0  # sliding window of outcomes, judged while closed
    open_seconds: float = 30.0  # time spent open before trial calls are allowed
    half_open_max_calls: int = 1  # trial calls in flight at once while half-open
    success_threshold: int = 1  # trial successes in a row that close the breaker
    slow_call_seconds: float | None = None  # a call slower than this counts as a failure
    ignored_exceptions: tuple[type[BaseException], ...] = ()  # caller errors, never counted

    def __post_init__(self) -> None:
        if not 0 < self.failure_rate_threshold <= 1:
            raise ValidationError("failure_rate_threshold must be in (0, 1]")
        if min(self.min_calls, self.half_open_max_calls, self.success_threshold) < 1:
            raise ValidationError("min_calls, half_open_max_calls and success_threshold must be >= 1")
        if self.window_seconds <= 0 or self.open_seconds <= 0:
            raise ValidationError("window_seconds and open_seconds must be positive")
        if self.slow_call_seconds is not None and self.slow_call_seconds <= 0:
            raise ValidationError("slow_call_seconds must be positive")


class CircuitOpenError(InvalidStateError):
    """Rejected without calling the dependency; ``retry_after`` says when a call may pass."""

    def __init__(self, name: str, state: State, retry_after: float) -> None:
        hint = f"retry after {retry_after:.1f} s" if retry_after > 0 else "a trial call is in flight"
        super().__init__(f"circuit {name!r} is {state.value}; {hint}")
        self.name = name
        self.state = state
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class Permit:
    """Proof that a call was admitted; hand it back together with the outcome."""

    generation: int  # the state episode that issued it; any transition invalidates it
    trial: bool  # a half-open trial call, occupying one of the limited slots
    issued_at: float


@dataclass(frozen=True, slots=True)
class BreakerStats:
    state: State
    calls: int  # outcomes in the sliding window
    failures: int
    rejected: int  # calls short-circuited since the breaker was created
    retry_after: float  # seconds until a call may pass; 0 unless open

    @property
    def failure_rate(self) -> float:
        return self.failures / self.calls if self.calls else 0.0


# --8<-- [end:policy]


# --8<-- [start:breaker]
class CircuitBreaker:
    """The state machine. ``_lock`` guards every field; the dependency runs outside it.

    CLOSED -> OPEN when the windowed failure rate trips; OPEN -> HALF_OPEN lazily, on the
    first call or state read after ``open_seconds`` (no timer thread); HALF_OPEN -> CLOSED
    after ``success_threshold`` trial successes; HALF_OPEN -> OPEN on one trial failure. Every
    transition bumps ``_generation`` and resets the trial counters, which is what makes an
    outcome reported for an earlier episode harmless.
    """

    def __init__(
        self,
        name: str,
        policy: BreakerPolicy | None = None,
        clock: Clock | None = None,
        on_transition: Callable[[str, State, State], None] | None = None,
    ) -> None:
        if not name:
            raise ValidationError("name must be non-empty")
        self._name = name
        self._policy = policy or BreakerPolicy()
        self._clock = clock or SystemClock()
        self._on_transition = on_transition
        self._lock = threading.Lock()
        self._state = State.CLOSED
        self._generation = 0
        self._window: deque[tuple[float, bool]] = deque()  # (recorded_at, ok) while closed
        self._window_failures = 0
        self._opened_at = 0.0
        self._trials_in_flight = 0
        self._trial_successes = 0
        self._rejected = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> State:
        with self._lock:
            event = self._tick(self._clock.now())
            state = self._state
        self._emit(event)
        return state

    def call[**P, R](self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        """Run ``fn`` through the breaker: admit, call outside the lock, record the outcome."""
        permit = self.acquire()
        try:
            result = fn(*args, **kwargs)
        except self._policy.ignored_exceptions:
            self.release(permit)
            raise
        except Exception:
            self.record(permit, ok=False)
            raise
        slow = self._policy.slow_call_seconds
        self.record(permit, ok=slow is None or self._clock.now() - permit.issued_at < slow)
        return result

    def acquire(self) -> Permit:
        """Admit a call or raise ``CircuitOpenError``; half-open admits a bounded number of trials."""
        with self._lock:
            now = self._clock.now()
            event = self._tick(now)
            outcome = self._admit_locked(now)
        self._emit(event)
        if isinstance(outcome, CircuitOpenError):
            raise outcome
        return outcome

    def record(self, permit: Permit, ok: bool) -> None:
        """Report the outcome of an admitted call (a timeout is a failure)."""
        self._settle(permit, ok)

    def release(self, permit: Permit) -> None:
        """Hand a permit back without a verdict: the failure was the caller's, not the dependency's."""
        self._settle(permit, None)

    def reset(self) -> None:
        """Operator override: close the breaker now and forget the window."""
        with self._lock:
            event = self._transition(State.CLOSED, self._clock.now())
        self._emit(event)

    def snapshot(self) -> BreakerStats:
        with self._lock:
            now = self._clock.now()
            event = self._tick(now)
            self._prune(now)
            retry_after = 0.0
            if self._state is State.OPEN:
                retry_after = max(0.0, self._opened_at + self._policy.open_seconds - now)
            stats = BreakerStats(
                self._state, len(self._window), self._window_failures, self._rejected, retry_after
            )
        self._emit(event)
        return stats

    # -- everything below runs under ``_lock`` ------------------------------------------------
    def _admit_locked(self, now: float) -> Permit | CircuitOpenError:
        policy = self._policy
        if self._state is State.OPEN:
            self._rejected += 1
            return CircuitOpenError(self._name, State.OPEN, self._opened_at + policy.open_seconds - now)
        if self._state is State.HALF_OPEN:
            if self._trials_in_flight >= policy.half_open_max_calls:
                self._rejected += 1
                return CircuitOpenError(self._name, State.HALF_OPEN, 0.0)
            self._trials_in_flight += 1
            return Permit(self._generation, trial=True, issued_at=now)
        return Permit(self._generation, trial=False, issued_at=now)

    def _settle(self, permit: Permit, ok: bool | None) -> None:
        with self._lock:
            event = self._settle_locked(permit, ok, self._clock.now())
        self._emit(event)

    def _settle_locked(self, permit: Permit, ok: bool | None, now: float) -> tuple[State, State] | None:
        if permit.generation != self._generation:
            return None  # an earlier episode: a reopen or a reset already reset the counters
        if permit.trial:
            self._trials_in_flight -= 1
        if ok is None:
            return None
        policy = self._policy
        if self._state is State.CLOSED:
            self._window.append((now, ok))
            if not ok:
                self._window_failures += 1
            self._prune(now)
            calls = len(self._window)
            if calls >= policy.min_calls and self._window_failures / calls >= policy.failure_rate_threshold:
                return self._transition(State.OPEN, now)
            return None
        if not ok:  # a half-open trial failed: straight back to open
            return self._transition(State.OPEN, now)
        self._trial_successes += 1
        if self._trial_successes >= policy.success_threshold:
            return self._transition(State.CLOSED, now)
        return None

    def _tick(self, now: float) -> tuple[State, State] | None:
        """The only timed transition: open -> half-open once ``open_seconds`` have passed."""
        if self._state is State.OPEN and now >= self._opened_at + self._policy.open_seconds:
            return self._transition(State.HALF_OPEN, now)
        return None

    def _prune(self, now: float) -> None:
        horizon = now - self._policy.window_seconds
        while self._window and self._window[0][0] <= horizon:
            _, ok = self._window.popleft()
            if not ok:
                self._window_failures -= 1

    def _transition(self, new: State, now: float) -> tuple[State, State]:
        old, self._state = self._state, new
        self._generation += 1
        self._trials_in_flight = 0
        self._trial_successes = 0
        if new is State.OPEN:
            self._opened_at = now
        elif new is State.CLOSED:
            self._window.clear()
            self._window_failures = 0
        return old, new

    def _emit(self, event: tuple[State, State] | None) -> None:
        """Listeners run outside the lock, so they may call back into the breaker."""
        if event is not None and event[0] is not event[1] and self._on_transition is not None:
            self._on_transition(self._name, *event)


# --8<-- [end:breaker]


def main() -> None:
    from concurrent.futures import ThreadPoolExecutor

    clock = FakeClock()
    policy = BreakerPolicy(
        failure_rate_threshold=0.5,
        min_calls=10,
        window_seconds=10.0,
        open_seconds=5.0,
        half_open_max_calls=1,
        success_threshold=2,
        slow_call_seconds=1.0,
        ignored_exceptions=(ValidationError,),
    )

    def announce(name: str, old: State, new: State) -> None:
        print(f"    {name}: {old.value} -> {new.value} at t={clock.now():.0f} s")

    breaker = CircuitBreaker("payments", policy, clock, on_transition=announce)
    reached = 0

    def payments(ok: bool = True) -> str:
        nonlocal reached
        reached += 1
        if not ok:
            raise TimeoutError("payments timed out")
        return "ok"

    def attempt(ok: bool = True) -> str:
        try:
            return breaker.call(payments, ok)
        except TimeoutError:
            return "failed"
        except CircuitOpenError as exc:
            return f"rejected, retry after {exc.retry_after:.0f} s"

    print(
        f"policy: open at >= {policy.failure_rate_threshold:.0%} failures over the last "
        f"{policy.window_seconds:.0f} s (min {policy.min_calls} calls); open for "
        f"{policy.open_seconds:.0f} s; {policy.half_open_max_calls} trial call at a time; "
        f"{policy.success_threshold} trial successes close"
    )
    print("t=0 s: 10 calls, 6 of them time out")
    results = [attempt(ok) for ok in (True, False, True, False, False, True, False, False, True, False)]
    stats = breaker.snapshot()
    print(
        f"  {results.count('ok')} ok, {results.count('failed')} failed; "
        f"failure rate {stats.failure_rate:.0%} over {stats.calls} calls -> {stats.state.value}"
    )
    results = [attempt() for _ in range(5)]
    print(f"t=0 s: 5 more calls: {len(results)} x '{results[0]}'; the dependency saw {reached} calls")
    clock.advance(5)
    print("t=5 s: the first call after open_seconds is the trial call, and it times out")
    print(f"  {attempt(False)}")
    clock.advance(5)
    print("t=10 s: two trial calls succeed")
    print(f"  {attempt()}, {attempt()}")

    def caller_bug() -> str:
        raise ValidationError("malformed request, the caller's fault")

    try:
        breaker.call(caller_bug)
    except ValidationError:
        pass
    print(f"a ValidationError passes through uncounted: window holds {breaker.snapshot().calls} calls")

    def slow_payment() -> str:
        clock.advance(1.5)
        return "ok"

    stats_before = breaker.snapshot()
    result = breaker.call(slow_payment)
    stats = breaker.snapshot()
    print(
        f"a 1.5 s call against a 1.0 s slow-call threshold returns '{result}' but counts as a "
        f"failure: window {stats.calls} call, {stats.failures - stats_before.failures} failure"
    )

    print("nine more timeouts reopen the breaker, then 1,600 calls arrive at once")
    for _ in range(9):
        attempt(False)
    before = reached
    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(1_600)))
    rejected = sum(1 for outcome in outcomes if outcome.startswith("rejected"))
    print(
        f"16 threads x 100 calls while open: {rejected} rejected in microseconds, "
        f"the dependency saw {reached - before} of them"
    )


if __name__ == "__main__":
    main()
