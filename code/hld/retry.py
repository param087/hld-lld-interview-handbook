"""Retries that calm an incident instead of amplifying it: backoff, jitter and a budget.

What the module demonstrates, in the order an interviewer asks about it:

* ``BackoffPolicy.delays`` yields the sleep before each retry under the four schemes every
  client library offers: none, full jitter, equal jitter and decorrelated jitter. Without
  jitter every client that failed together retries together, and the dependency gets the same
  synchronised wave it just fell over under.
* ``RetryBudget`` is the throttle gRPC and Envoy use: retries spend tokens, successes earn
  them back slowly, and retries stop while the bucket is less than half full. It is what turns
  off retries during a total outage, when they are pure amplification.
* ``Retrier.call`` puts the two together, retries only the exceptions you name, and keeps
  aggregate ``RetryStats`` so you can see the retry rate a fleet is really generating.
* ``retry`` is the same machine as a decorator.

Time and randomness are injected (``sleep``, ``rng``), so tests are deterministic and the
demo simulates a 200-client retry storm in microseconds. ``_lock`` guards the stats and the
random number generator; ``RetryBudget`` has its own lock over its token count.
"""

from __future__ import annotations

import functools
import random
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from common import ValidationError

# Errors worth retrying: the call may not have reached the dependency, or it timed out.
TRANSIENT: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)


# --8<-- [start:backoff]
class Jitter(StrEnum):
    """How much randomness to mix into the exponential delay."""

    NONE = "none"  # sleep = exp; synchronised clients stay synchronised
    FULL = "full"  # sleep = U(0, exp); the best spread, the shortest mean wait
    EQUAL = "equal"  # sleep = exp/2 + U(0, exp/2); spread with a guaranteed minimum pause
    DECORRELATED = "decorrelated"  # sleep = min(cap, U(base, 3 * previous)); grows on its own


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential backoff with a cap, a bounded number of attempts and a jitter scheme.

    ``max_attempts`` counts the first call, so 4 attempts means 1 call and up to 3 retries.
    Cap the delay: uncapped doubling from 100 ms reaches 100 ms x 2^9 = 51 s by attempt 10,
    long past any request timeout the caller is willing to hold.
    """

    base_seconds: float = 0.1
    cap_seconds: float = 10.0
    multiplier: float = 2.0
    max_attempts: int = 4
    jitter: Jitter = Jitter.FULL

    def __post_init__(self) -> None:
        if self.base_seconds <= 0 or self.cap_seconds < self.base_seconds:
            raise ValidationError("need 0 < base_seconds <= cap_seconds")
        if self.multiplier < 1:
            raise ValidationError("multiplier must be >= 1")
        if self.max_attempts < 1:
            raise ValidationError("max_attempts must be >= 1")

    def exponential(self, attempt: int) -> float:
        """The unjittered delay before retry ``attempt`` (1-based): min(cap, base * m^(n-1))."""
        if attempt < 1:
            raise ValidationError("attempt is 1-based")
        return min(self.cap_seconds, self.base_seconds * self.multiplier ** (attempt - 1))

    def delays(self, rng: random.Random) -> Iterator[float]:
        """The ``max_attempts - 1`` sleeps between attempts, drawn from ``rng``."""
        previous = self.base_seconds
        for attempt in range(1, self.max_attempts):
            exp = self.exponential(attempt)
            match self.jitter:
                case Jitter.NONE:
                    sleep = exp
                case Jitter.FULL:
                    sleep = rng.uniform(0.0, exp)
                case Jitter.EQUAL:
                    sleep = exp / 2 + rng.uniform(0.0, exp / 2)
                case Jitter.DECORRELATED:
                    sleep = min(self.cap_seconds, rng.uniform(self.base_seconds, previous * 3))
            previous = sleep
            yield sleep


# --8<-- [end:backoff]


# --8<-- [start:budget]
class RetryBudget:
    """A token bucket that caps retries as a *fraction* of traffic, not per call.

    Per-call limits do not bound the amplification: 3 attempts each turns a dependency's bad
    minute into 3x the load exactly when it can least take it. A budget does. Every failed
    attempt spends a token, every success earns ``token_ratio`` back, and retries stop while
    the bucket is under half full, so a healthy service retries freely and a dead one is not
    retried at all. With the gRPC defaults (100 tokens, ratio 0.1) a total outage allows ~50
    retries before the throttle closes; steady state allows one retry per 10 successes, and
    the extra load a retrying fleet can add is bounded at ~10%.

    ``_lock`` guards ``_tokens``.
    """

    def __init__(self, max_tokens: float = 100.0, token_ratio: float = 0.1) -> None:
        if max_tokens <= 0:
            raise ValidationError("max_tokens must be positive")
        if not 0 < token_ratio <= 1:
            raise ValidationError("token_ratio must be in (0, 1]")
        self._max_tokens = float(max_tokens)
        self._token_ratio = float(token_ratio)
        self._tokens = float(max_tokens)
        self._lock = threading.Lock()

    @property
    def tokens(self) -> float:
        with self._lock:
            return self._tokens

    @property
    def max_tokens(self) -> float:
        return self._max_tokens

    def allows_retry(self) -> bool:
        """Retries are allowed only while the bucket is more than half full."""
        with self._lock:
            return self._tokens > self._max_tokens / 2

    def record(self, ok: bool) -> None:
        """One attempt's outcome: a failure costs a token, a success earns ``token_ratio``."""
        with self._lock:
            delta = self._token_ratio if ok else -1.0
            self._tokens = max(0.0, min(self._max_tokens, self._tokens + delta))


# --8<-- [end:budget]


# --8<-- [start:retrier]
@dataclass(frozen=True, slots=True)
class RetryStats:
    """Aggregate counters for one ``Retrier``, which usually guards one dependency."""

    calls: int  # calls made through the retrier
    attempts: int  # individual invocations of the wrapped callable
    failures: int  # attempts that raised a retryable exception
    slept_seconds: float
    exhausted: int  # calls that used every attempt and still failed
    budget_denied: int  # calls that stopped early because the budget was empty

    @property
    def retries(self) -> int:
        return self.attempts - self.calls

    @property
    def amplification(self) -> float:
        """Attempts per call: what the dependency sees for every request the caller makes."""
        return self.attempts / self.calls if self.calls else 1.0


class Retrier:
    """Runs a callable until it succeeds, the attempts run out or the budget says stop.

    Retry only what is safe to repeat. A retried write that already committed is a duplicate
    charge, so a retryable write carries an idempotency key and the server deduplicates on it;
    without that, retry reads and leave writes to a single attempt plus a queue.

    ``_lock`` guards the counters and ``_rng``: the whole delay sequence for one call is drawn
    in one locked step, so a seeded generator makes single-threaded runs reproducible.
    """

    def __init__(
        self,
        policy: BackoffPolicy | None = None,
        *,
        retry_on: tuple[type[BaseException], ...] = TRANSIENT,
        budget: RetryBudget | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not retry_on:
            raise ValidationError("retry_on must name at least one exception type")
        self._policy = policy or BackoffPolicy()
        self._retry_on = retry_on
        self._budget = budget
        self._rng = rng or random.Random()
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._calls = self._attempts = self._failures = 0
        self._exhausted = self._denied = 0
        self._slept = 0.0

    @property
    def policy(self) -> BackoffPolicy:
        return self._policy

    def stats(self) -> RetryStats:
        with self._lock:
            return RetryStats(
                self._calls, self._attempts, self._failures, self._slept, self._exhausted, self._denied
            )

    def call[**P, R](self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        """Call ``fn``, retrying the named exceptions; the last failure is re-raised as it is."""
        with self._lock:
            self._calls += 1
            delays = list(self._policy.delays(self._rng))
        for attempt in range(1, self._policy.max_attempts + 1):
            with self._lock:
                self._attempts += 1
            try:
                result = fn(*args, **kwargs)
            except self._retry_on:
                with self._lock:
                    self._failures += 1
                if self._budget is not None:
                    self._budget.record(ok=False)
                if attempt == self._policy.max_attempts:
                    with self._lock:
                        self._exhausted += 1
                    raise
                if self._budget is not None and not self._budget.allows_retry():
                    with self._lock:
                        self._denied += 1
                    raise
                pause = delays[attempt - 1]
                self._sleep(pause)
                with self._lock:
                    self._slept += pause
                continue
            if self._budget is not None:
                self._budget.record(ok=True)
            return result
        raise ValidationError("unreachable: max_attempts must be >= 1")  # pragma: no cover

    def decorate[**P, R](self, fn: Callable[P, R]) -> Callable[P, R]:
        """Wrap ``fn`` so every call goes through this retrier (and shares its stats)."""

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return self.call(fn, *args, **kwargs)

        return wrapper


def retry[**P, R](
    policy: BackoffPolicy | None = None,
    *,
    retry_on: tuple[type[BaseException], ...] = TRANSIENT,
    budget: RetryBudget | None = None,
    rng: random.Random | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator form: ``@retry(BackoffPolicy(jitter=Jitter.FULL), budget=shared_budget)``."""
    return Retrier(policy, retry_on=retry_on, budget=budget, rng=rng, sleep=sleep).decorate


# --8<-- [end:retrier]


def storm_spread(
    clients: int, delay: float, jitter: Jitter, rng: random.Random, bucket: float
) -> int:
    """Worst-case clients retrying in the same ``bucket``-wide window after a shared failure."""
    policy = BackoffPolicy(base_seconds=delay, cap_seconds=delay, multiplier=1.0, max_attempts=2, jitter=jitter)
    buckets: dict[int, int] = {}
    for _ in range(clients):
        when = next(policy.delays(rng))
        index = int(when / bucket)
        buckets[index] = buckets.get(index, 0) + 1
    return max(buckets.values())


def main() -> None:
    base, cap = 0.1, 10.0
    print(f"backoff from base {base * 1000:.0f} ms, cap {cap:.0f} s, 6 attempts")
    for jitter in Jitter:
        policy = BackoffPolicy(base_seconds=base, cap_seconds=cap, max_attempts=6, jitter=jitter)
        delays = list(policy.delays(random.Random(42)))
        pretty = " ".join(f"{d * 1000:7.0f}" for d in delays)
        print(f"  {jitter.value:<13} {pretty}   total {sum(delays):5.2f} s")

    print("\n200 clients fail together; attempt 3 sleeps 400 ms; count the worst 20 ms window")
    for jitter in (Jitter.NONE, Jitter.FULL, Jitter.EQUAL):
        peak = storm_spread(200, 0.4, jitter, random.Random(42), bucket=0.02)
        print(f"  {jitter.value:<13} at most {peak:3d} of 200 clients retry in the same 20 ms")

    slept = 0.0

    def fake_sleep(seconds: float) -> None:
        nonlocal slept
        slept += seconds

    attempts = 0

    def flaky(fail_times: int) -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= fail_times:
            raise TimeoutError("upstream timed out")
        return "ok"

    policy = BackoffPolicy(base_seconds=base, cap_seconds=cap, max_attempts=4, jitter=Jitter.FULL)
    retrier = Retrier(policy, rng=random.Random(7), sleep=fake_sleep)
    print(f"\ntwo transient timeouts then success -> {retrier.call(flaky, 2)!r}")
    stats = retrier.stats()
    print(
        f"  {stats.attempts} attempts for {stats.calls} call "
        f"(amplification {stats.amplification:.1f}x), slept {stats.slept_seconds * 1000:.0f} ms"
    )

    budget = RetryBudget(max_tokens=100.0, token_ratio=0.1)
    dead = Retrier(policy, budget=budget, rng=random.Random(7), sleep=fake_sleep)

    def down() -> str:
        raise ConnectionError("dependency is down")

    closed_after = 0
    for call in range(1, 101):
        try:
            dead.call(down)
        except ConnectionError:
            pass
        if closed_after == 0 and dead.stats().budget_denied:
            closed_after = call
    stats = dead.stats()
    print(
        f"\ntotal outage, {budget.max_tokens:.0f}-token budget, 100 calls: the throttle closed "
        f"on call {closed_after}, at {budget.tokens:.0f} tokens left"
    )
    print(
        f"  {stats.attempts} attempts for {stats.calls} calls = {stats.amplification:.2f}x load "
        f"on a dying dependency, instead of {policy.max_attempts}x without a budget"
    )


if __name__ == "__main__":
    main()
