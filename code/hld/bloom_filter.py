"""Bloom filter (and a counting variant): set membership in about ten bits per item.

A Bloom filter answers "definitely not in the set" or "probably in the set". It keeps ``m``
bits and maps every item to ``k`` positions: ``add`` sets them, a lookup checks them. There
are no false negatives; a false positive happens when other items happen to have set all
``k`` bits. After ``n`` insertions the false-positive rate is ``(1 - e^(-kn/m))^k``, so size
the filter with ``optimal_size``: ``m = -n ln p / (ln 2)^2`` bits and ``k = (m/n) ln 2``
hash functions. ``CountingBloomFilter`` swaps bits for small counters so items can be removed.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Iterator

from common import ValidationError


# --8<-- [start:sizing]
def optimal_size(capacity: int, error_rate: float) -> tuple[int, int]:
    """``(m bits, k hashes)`` for ``capacity`` items at ``error_rate`` false positives.

    10,000 items at 1% need 95,851 bits (11.7 KB) and 7 hashes: ~9.6 bits per item whatever
    the items' size, which is the whole point.
    """
    if capacity <= 0:
        raise ValidationError("capacity must be positive")
    if not 0.0 < error_rate < 1.0:
        raise ValidationError("error_rate must be in (0, 1)")
    bits = math.ceil(-capacity * math.log(error_rate) / math.log(2) ** 2)
    hashes = max(1, round(bits / capacity * math.log(2)))
    return bits, hashes


def false_positive_rate(bits: int, hashes: int, items: int) -> float:
    """The textbook rate after ``items`` insertions: ``(1 - e^(-k n / m))^k``."""
    return (1.0 - math.exp(-hashes * items / bits)) ** hashes


def positions(item: str, hashes: int, bits: int) -> Iterator[int]:
    """``k`` positions from one digest by double hashing: ``h1 + i * h2 (mod m)``.

    Kirsch and Mitzenmacher showed this matches ``k`` independent hash functions in
    false-positive rate, and it hashes each item once instead of ``k`` times.
    """
    digest = hashlib.md5(item.encode(), usedforsecurity=False).digest()
    h1 = int.from_bytes(digest[:8], "big")
    h2 = int.from_bytes(digest[8:], "big") | 1  # odd, so the stride is never zero
    return ((h1 + i * h2) % bits for i in range(hashes))


# --8<-- [end:sizing]
# --8<-- [start:bloom]
class BloomFilter:
    """``m`` bits in a ``bytearray``. ``_lock`` guards the bits and the item counter: setting
    a bit is a read-modify-write of its byte. Lookups read without the lock (a byte read is
    atomic), so a concurrent lookup sees either the old or the new bit, never garbage."""

    def __init__(self, capacity: int, error_rate: float = 0.01) -> None:
        self.bits, self.hashes = optimal_size(capacity, error_rate)
        self.capacity = capacity
        self._bytes = bytearray((self.bits + 7) // 8)
        self._items = 0
        self._lock = threading.Lock()

    def add(self, item: str) -> None:
        with self._lock:
            for pos in positions(item, self.hashes, self.bits):
                self._bytes[pos >> 3] |= 1 << (pos & 7)
            self._items += 1

    def __contains__(self, item: str) -> bool:
        """``False`` is certain; ``True`` means "probably": all ``k`` bits are set."""
        slots = positions(item, self.hashes, self.bits)
        return all(self._bytes[pos >> 3] >> (pos & 7) & 1 for pos in slots)

    def __len__(self) -> int:
        """Insertions so far (a duplicate counts again: the filter cannot tell)."""
        return self._items

    @property
    def fill_ratio(self) -> float:
        """Fraction of bits set; the live false-positive rate is about ``fill_ratio ** k``."""
        return sum(byte.bit_count() for byte in self._bytes) / self.bits

    @property
    def expected_error_rate(self) -> float:
        """The formula's rate for the current number of insertions."""
        return false_positive_rate(self.bits, self.hashes, self._items)

    def memory_bytes(self) -> int:
        return len(self._bytes)


# --8<-- [end:bloom]
# --8<-- [start:counting]
class CountingBloomFilter:
    """Counters instead of bits (4-bit in the literature, a byte here) so ``remove`` works.

    Removing an item that was never added would corrupt other items' counters, so ``remove``
    refuses when any of the item's counters is zero; a counter saturates at 255 instead of
    wrapping and is then never decremented. ``_lock`` guards the counters.
    """

    def __init__(self, capacity: int, error_rate: float = 0.01) -> None:
        self.slots, self.hashes = optimal_size(capacity, error_rate)
        self._counters = bytearray(self.slots)
        self._lock = threading.Lock()

    def add(self, item: str) -> None:
        with self._lock:
            for pos in positions(item, self.hashes, self.slots):
                if self._counters[pos] < 255:
                    self._counters[pos] += 1

    def remove(self, item: str) -> None:
        with self._lock:
            slots = list(positions(item, self.hashes, self.slots))
            if any(self._counters[pos] == 0 for pos in slots):
                raise ValidationError(f"{item!r} was never added")
            for pos in slots:
                if self._counters[pos] < 255:
                    self._counters[pos] -= 1

    def __contains__(self, item: str) -> bool:
        return all(self._counters[pos] for pos in positions(item, self.hashes, self.slots))

    def memory_bytes(self) -> int:
        return len(self._counters)


# --8<-- [end:counting]


def main() -> None:
    capacity, target = 10_000, 0.01
    bits, hashes = optimal_size(capacity, target)
    print(
        f"sizing: {capacity:,} items at {target:.0%} -> m={bits:,} bits "
        f"({bits / 8 / 1024:.1f} KB), k={hashes}, {bits / capacity:.1f} bits per item"
    )
    bloom = BloomFilter(capacity, target)
    members = [f"user:{i}" for i in range(capacity)]
    for item in members:
        bloom.add(item)
    found = sum(item in bloom for item in members)
    print(f"no false negatives: {found:,}/{capacity:,} members found")

    probes = [f"other:{i}" for i in range(100_000)]
    measured = sum(item in bloom for item in probes) / len(probes)
    print(
        f"false positives at capacity: {measured:.2%} measured vs "
        f"{bloom.expected_error_rate:.2%} formula, fill ratio {bloom.fill_ratio:.3f}"
    )
    for item in (f"user:{i}" for i in range(capacity, 2 * capacity)):
        bloom.add(item)
    measured = sum(item in bloom for item in probes) / len(probes)
    print(
        f"false positives at 2x capacity: {measured:.1%} measured vs "
        f"{bloom.expected_error_rate:.1%} formula (size for the peak, not the average)"
    )

    counting = CountingBloomFilter(capacity, target)
    for item in ("alice", "bob", "carol"):
        counting.add(item)
    counting.remove("bob")
    print(
        f"counting filter: {counting.memory_bytes() / 1024:.1f} KB, after remove('bob'): "
        f"alice={'alice' in counting} bob={'bob' in counting} carol={'carol' in counting}"
    )


if __name__ == "__main__":
    main()
