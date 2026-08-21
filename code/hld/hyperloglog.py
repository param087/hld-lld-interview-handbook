"""HyperLogLog: count distinct items in a few kilobytes with about one percent error.

Hash each item to 64 bits. The first ``p`` bits pick one of ``m = 2^p`` registers; the
register keeps the longest run of leading zeros (plus one) seen in the remaining bits. A run
of ``r`` zeros is a 1-in-2^r event, so together the registers estimate the cardinality as a
harmonic mean, ``alpha_m * m^2 / sum(2^-M[j])``, with a relative standard error of
``1.04 / sqrt(m)``: 16,384 one-byte registers (16 KB) give ~0.8%. Small cardinalities use
linear counting on the still-empty registers. Registers merge by element-wise ``max``, so
per-shard sketches combine into one distinct count without moving any items.
"""

from __future__ import annotations

import hashlib
import math
import threading

from common import ValidationError


# --8<-- [start:hll]
def alpha(registers: int) -> float:
    """Bias correction constant for ``m`` registers (Flajolet et al., 2007)."""
    if registers == 16:
        return 0.673
    if registers == 32:
        return 0.697
    if registers == 64:
        return 0.709
    return 0.7213 / (1.0 + 1.079 / registers)


class HyperLogLog:
    """``m = 2^precision`` one-byte registers. ``_lock`` guards them: ``add`` is a
    read-max-write and ``merge`` rewrites them all; ``count`` snapshots the bytes first."""

    def __init__(self, precision: int = 14) -> None:
        if not 4 <= precision <= 18:
            raise ValidationError("precision must be between 4 and 18")
        self.precision = precision
        self.registers = 1 << precision
        self._registers = bytearray(self.registers)
        self._lock = threading.Lock()

    @property
    def error_bound(self) -> float:
        """Relative standard error of the estimate: ``1.04 / sqrt(m)``."""
        return 1.04 / math.sqrt(self.registers)

    def memory_bytes(self) -> int:
        return len(self._registers)

    def add(self, item: str) -> None:
        digest = hashlib.md5(item.encode(), usedforsecurity=False).digest()
        value = int.from_bytes(digest[:8], "big")
        index = value >> (64 - self.precision)
        rest_bits = 64 - self.precision
        rest = value & ((1 << rest_bits) - 1)
        rank = rest_bits - rest.bit_length() + 1  # leading zeros in the rest, plus one
        with self._lock:
            if rank > self._registers[index]:
                self._registers[index] = rank

    def count(self) -> int:
        """The cardinality estimate; ``2.5 m`` is the cut-over from linear counting."""
        registers = bytes(self._registers)
        m = self.registers
        raw = alpha(m) * m * m / sum(2.0 ** -rank for rank in registers)
        zeros = registers.count(0)
        if raw <= 2.5 * m and zeros:
            return round(m * math.log(m / zeros))
        return round(raw)

    def merge(self, other: HyperLogLog) -> None:
        """Union: keep the larger rank per register; the estimate of the union follows."""
        if other is self:
            return
        if other.precision != self.precision:
            raise ValidationError("sketches must share a precision to merge")
        first, second = sorted((self, other), key=id)  # fixed lock order, no deadlock
        with first._lock, second._lock:
            pairs = zip(self._registers, other._registers, strict=True)
            self._registers[:] = bytes(max(a, b) for a, b in pairs)


# --8<-- [end:hll]


def main() -> None:
    def report(label: str, hll: HyperLogLog, exact: int) -> None:
        estimate = hll.count()
        error = abs(estimate - exact) / exact
        print(f"{label}: estimate {estimate:,} vs exact {exact:,} (error {error:.2%})")

    distinct = 200_000
    hll14 = HyperLogLog(precision=14)
    hll10 = HyperLogLog(precision=10)
    for i in range(distinct):
        for sketch in (hll14, hll10):
            sketch.add(f"user:{i}")
            if i % 2 == 0:  # every second user comes back: duplicates must not count
                sketch.add(f"user:{i}")
    print(
        f"p=14: m={hll14.registers:,} registers ({hll14.memory_bytes() / 1024:.0f} KB), "
        f"error bound {hll14.error_bound:.2%}; p=10: m={hll10.registers:,} "
        f"({hll10.memory_bytes() / 1024:.0f} KB), bound {hll10.error_bound:.2%}"
    )
    report("p=14, 300,000 adds", hll14, distinct)
    report("p=10, 300,000 adds", hll10, distinct)

    small = HyperLogLog(precision=14)
    for i in range(1_000):
        small.add(f"s:{i}")
    report("p=14, small range (linear counting)", small, 1_000)

    shard_a, shard_b = HyperLogLog(precision=14), HyperLogLog(precision=14)
    for i in range(60_000):
        shard_a.add(f"u:{i}")
    for i in range(40_000, 100_000):
        shard_b.add(f"u:{i}")
    shard_a.merge(shard_b)
    report("merge of two shards (60k + 60k, 20k shared)", shard_a, 100_000)


if __name__ == "__main__":
    main()
