"""Snowflake-style 64-bit ids: 41-bit timestamp, 10-bit machine id, 12-bit sequence.

The crux of the unique-id-generator design in one module:

* ``Layout`` is the bit split (41/10/12 by default) with the derived shifts, masks and ranges,
  plus ``compose`` / ``decompose`` so an id can be split back into its parts for debugging.
* ``SnowflakeGenerator`` mints ids without any network call. One lock guards the pair
  ``(last_ms, sequence)``. When the sequence overflows inside one millisecond the generator
  *borrows* the next millisecond instead of spinning, and when the wall clock moves backwards
  it keeps issuing from its own logical millisecond. Both are bounded by ``max_drift_ms``:
  beyond that it raises ``ClockDriftError`` rather than risk a duplicate.
* ``MachineIdRegistry`` stands in for the ZooKeeper/etcd lease that hands every worker a
  distinct machine id: lowest free id, ephemeral lease, idempotent re-registration after a
  fast restart, and a renew that fails loudly once the lease is lost (fencing).

Public API reused by the URL-shortener case study: ``SnowflakeGenerator``, ``Layout``,
``SnowflakeParts``, ``DEFAULT_EPOCH_MS``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from common import (
    Clock,
    ConflictError,
    HandbookError,
    InvalidStateError,
    SystemClock,
    ValidationError,
)

DEFAULT_EPOCH_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z; a custom epoch buys ~69 years


class ClockDriftError(HandbookError):
    """The generator's logical clock is further ahead of the wall clock than it tolerates."""


# --8<-- [start:layout]
@dataclass(frozen=True, slots=True)
class SnowflakeParts:
    timestamp_ms: int  # absolute Unix milliseconds (epoch added back)
    machine_id: int
    sequence: int

    @property
    def created_at(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp_ms / 1000, tz=UTC)


@dataclass(frozen=True, slots=True)
class Layout:
    """Bit split of a 63-bit positive id: ``[timestamp | machine | sequence]``.

    The sign bit stays 0 so the id is a positive ``bigint`` in every database and language.
    """

    timestamp_bits: int = 41
    machine_bits: int = 10
    sequence_bits: int = 12

    def __post_init__(self) -> None:
        if min(self.timestamp_bits, self.machine_bits, self.sequence_bits) <= 0:
            raise ValidationError("every field needs at least one bit")
        if self.timestamp_bits + self.machine_bits + self.sequence_bits != 63:
            raise ValidationError("timestamp + machine + sequence bits must total 63")

    @property
    def max_machine_id(self) -> int:
        return (1 << self.machine_bits) - 1

    @property
    def max_sequence(self) -> int:
        return (1 << self.sequence_bits) - 1

    @property
    def lifetime_ms(self) -> int:
        """How long the timestamp field lasts: 2^41 ms is about 69 years."""
        return 1 << self.timestamp_bits

    @property
    def machine_shift(self) -> int:
        return self.sequence_bits

    @property
    def timestamp_shift(self) -> int:
        return self.sequence_bits + self.machine_bits

    def compose(self, elapsed_ms: int, machine_id: int, sequence: int) -> int:
        return (elapsed_ms << self.timestamp_shift) | (machine_id << self.machine_shift) | sequence

    def decompose(self, snowflake_id: int, epoch_ms: int = DEFAULT_EPOCH_MS) -> SnowflakeParts:
        if snowflake_id < 0:
            raise ValidationError("snowflake ids are positive")
        return SnowflakeParts(
            timestamp_ms=(snowflake_id >> self.timestamp_shift) + epoch_ms,
            machine_id=(snowflake_id >> self.machine_shift) & self.max_machine_id,
            sequence=snowflake_id & self.max_sequence,
        )


# --8<-- [end:layout]


# --8<-- [start:generator]
class SnowflakeGenerator:
    """One generator per (process, machine id). ``_lock`` guards ``_last_ms`` and ``_sequence``.

    ``_last_ms`` is a *logical* millisecond: it never moves backwards, which is what makes the
    ids strictly increasing on one machine even when the wall clock is not. The wall clock only
    pulls it forward. The gap ``_last_ms - wall`` is the drift budget: sequence overflow spends
    1 ms of it, a backwards clock step spends as many ms as the step, and beyond
    ``max_drift_ms`` the generator refuses to mint (the caller retries or fails over).
    """

    def __init__(
        self,
        machine_id: int,
        clock: Clock | None = None,
        layout: Layout | None = None,
        epoch_ms: int = DEFAULT_EPOCH_MS,
        max_drift_ms: int = 10,
    ) -> None:
        self._layout = layout or Layout()
        if not 0 <= machine_id <= self._layout.max_machine_id:
            raise ValidationError(f"machine_id must be in 0..{self._layout.max_machine_id}")
        if max_drift_ms < 0:
            raise ValidationError("max_drift_ms must be >= 0")
        self._machine_id = machine_id
        self._clock = clock or SystemClock()
        self._epoch_ms = epoch_ms
        self._max_drift_ms = max_drift_ms
        self._last_ms = -1
        self._sequence = 0
        self._borrowed_ms = 0
        self._lock = threading.Lock()

    @property
    def machine_id(self) -> int:
        return self._machine_id

    @property
    def borrowed_ms(self) -> int:
        """How many milliseconds the logical clock has run ahead because of sequence overflow."""
        return self._borrowed_ms

    def _wall_ms(self) -> int:
        elapsed = round(self._clock.now() * 1000) - self._epoch_ms
        if elapsed < 0:
            raise InvalidStateError("wall clock is before the custom epoch")
        if elapsed >= self._layout.lifetime_ms:
            raise InvalidStateError("timestamp field exhausted; a new epoch and layout are needed")
        return elapsed

    def next_id(self) -> int:
        with self._lock:
            wall = self._wall_ms()
            borrowed = 0
            if wall > self._last_ms:
                last_ms, sequence = wall, 0
            else:
                # same millisecond, or the wall clock stepped backwards: stay on the logical clock
                last_ms, sequence = self._last_ms, self._sequence + 1
                if sequence > self._layout.max_sequence:
                    last_ms, sequence, borrowed = last_ms + 1, 0, 1  # borrow the next ms, no spin
                if last_ms - wall > self._max_drift_ms:
                    raise ClockDriftError(
                        f"logical clock would be {last_ms - wall} ms ahead of the wall clock "
                        f"(limit {self._max_drift_ms} ms); refusing to mint"
                    )
            self._last_ms, self._sequence = last_ms, sequence
            self._borrowed_ms += borrowed
            return self._layout.compose(last_ms, self._machine_id, sequence)

    def decompose(self, snowflake_id: int) -> SnowflakeParts:
        return self._layout.decompose(snowflake_id, self._epoch_ms)


# --8<-- [end:generator]


# --8<-- [start:registry]
@dataclass(slots=True)
class MachineLease:
    machine_id: int
    owner: str
    expires_at: float


class MachineIdRegistry:
    """Stand-in for ZooKeeper/etcd ephemeral nodes under ``/snowflake/machines``.

    A worker calls ``register`` at start-up, then ``renew`` well inside ``lease_seconds``. A
    worker that misses its renewal loses the id and must stop minting before anyone else can
    be granted it; the second owner only appears after the lease has expired. ``_lock`` guards
    ``_leases``.
    """

    def __init__(self, capacity: int = 1024, lease_seconds: float = 30.0, clock: Clock | None = None) -> None:
        if capacity <= 0 or lease_seconds <= 0:
            raise ValidationError("capacity and lease_seconds must be positive")
        self._capacity = capacity
        self._lease_seconds = lease_seconds
        self._clock = clock or SystemClock()
        self._leases: dict[int, MachineLease] = {}
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        for machine_id in [m for m, lease in self._leases.items() if lease.expires_at <= now]:
            del self._leases[machine_id]

    def register(self, owner: str) -> int:
        """Lowest free machine id; idempotent for an owner whose lease is still alive."""
        with self._lock:
            now = self._clock.now()
            self._expire(now)
            for lease in self._leases.values():
                if lease.owner == owner:  # fast restart: the old lease is still ours
                    lease.expires_at = now + self._lease_seconds
                    return lease.machine_id
            for machine_id in range(self._capacity):
                if machine_id not in self._leases:
                    self._leases[machine_id] = MachineLease(machine_id, owner, now + self._lease_seconds)
                    return machine_id
            raise ConflictError(f"all {self._capacity} machine ids are leased")

    def renew(self, owner: str, machine_id: int) -> None:
        with self._lock:
            now = self._clock.now()
            self._expire(now)
            lease = self._leases.get(machine_id)
            if lease is None or lease.owner != owner:
                raise InvalidStateError(f"{owner} no longer holds machine id {machine_id}; stop minting")
            lease.expires_at = now + self._lease_seconds

    def release(self, owner: str, machine_id: int) -> None:
        with self._lock:
            lease = self._leases.get(machine_id)
            if lease is not None and lease.owner == owner:
                del self._leases[machine_id]

    def holder(self, machine_id: int) -> str | None:
        with self._lock:
            self._expire(self._clock.now())
            lease = self._leases.get(machine_id)
            return lease.owner if lease else None


# --8<-- [end:registry]


def main() -> None:
    from common import FakeClock

    layout = Layout()
    print(f"layout: {layout.timestamp_bits}/{layout.machine_bits}/{layout.sequence_bits} bits, "
          f"{layout.max_machine_id + 1} machines, {layout.max_sequence + 1} ids/ms/machine, "
          f"{layout.lifetime_ms // 31_500_000_000} years of timestamps")

    clock = FakeClock(start=1_750_000_000.0)
    registry = MachineIdRegistry(lease_seconds=30, clock=clock)
    a, b = registry.register("worker-a"), registry.register("worker-b")
    print(f"registry: worker-a -> machine {a}, worker-b -> machine {b}, "
          f"worker-a restarts -> machine {registry.register('worker-a')}")

    gen = SnowflakeGenerator(machine_id=a, clock=clock, max_drift_ms=5)
    for _ in range(3):
        snowflake_id = gen.next_id()
        parts = gen.decompose(snowflake_id)
        stamp = parts.created_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        print(f"  id={snowflake_id} -> {stamp}Z machine={parts.machine_id} seq={parts.sequence}")

    clock.advance(0.001)
    burst = [gen.next_id() for _ in range(5000)]
    first, last = gen.decompose(burst[0]), gen.decompose(burst[-1])
    print(f"burst: 5000 ids in one frozen ms -> unique={len(set(burst)) == 5000}, "
          f"sorted={burst == sorted(burst)}, borrowed {gen.borrowed_ms} ms "
          f"(last id sits at +{last.timestamp_ms - first.timestamp_ms} ms, seq {last.sequence})")

    clock.set(clock.now() - 0.003)
    after_step = gen.next_id()
    print(f"clock steps back 3 ms: next id still increasing={after_step > burst[-1]}")
    clock.set(clock.now() - 0.050)
    try:
        gen.next_id()
    except ClockDriftError as exc:
        print(f"clock steps back 50 ms: ClockDriftError: {exc}")

    clock.advance(20)
    registry.renew("worker-a", a)
    clock.advance(11)
    print(f"lease expiry: worker-a renewed, worker-b did not; after 31 s machine {b} is held by "
          f"{registry.holder(b)} and worker-c gets machine {registry.register('worker-c')}")
    try:
        registry.renew("worker-b", b)
    except InvalidStateError as exc:
        print(f"worker-b renew -> InvalidStateError: {exc}")


if __name__ == "__main__":
    main()
