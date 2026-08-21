"""Short codes for a URL shortener: base62 over a counter, over a Snowflake id, or over a hash.

The crux of the URL-shortener design in one module. Every strategy answers the same question --
"which seven characters do I hand back?" -- and differs only in where the number comes from:

* ``CounterCodes``: a monotonic counter run through a 40-bit Feistel permutation, then base62.
  Collision-free by construction, densest use of the code space, but the counter is shared state
  and consecutive codes would be guessable without the permutation.
* ``SnowflakeCodes``: a 64-bit Snowflake id (see ``hld.snowflake``) base62-encoded. No shared
  counter at all, at the price of a longer code, because the id carries a timestamp.
* ``HashCodes``: the first seven base62 digits of ``sha256(url)``, retried with a salt when the
  code is taken. Deduplicates identical URLs for free and needs no coordination, but collisions
  are certain once the table is large (the birthday bound), so every write is conditional.

All three share ``ShortCodeStrategy.shorten()``, which claims the code with a single conditional
write (``INSERT ... ON CONFLICT DO NOTHING``, DynamoDB ``attribute_not_exists``, Redis ``SETNX``)
and retries only when someone else already owns it. ``InMemoryCodeStore`` stands in for that
store; its ``_lock`` guards ``_rows`` and makes ``claim`` the atomic check-and-set the real
database gives you for free.
"""

from __future__ import annotations

import hashlib
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from common import Clock, ConflictError, ValidationError
from hld.snowflake import SnowflakeGenerator

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)
CODE_LENGTH = 7  # 62^7 = 3.5 x 10^12 codes
_INDEX: Mapping[str, int] = MappingProxyType({char: i for i, char in enumerate(ALPHABET)})


# --8<-- [start:codec]
def encode(number: int, width: int = 0) -> str:
    """Base62 digits of a non-negative integer, left-padded to ``width`` characters."""
    if number < 0:
        raise ValidationError("base62 encodes non-negative integers only")
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, BASE)
        digits.append(ALPHABET[remainder])
    return "".join(reversed(digits)).rjust(max(width, 1), ALPHABET[0])


def decode(code: str) -> int:
    """Inverse of :func:`encode`. Leading padding characters are harmless (they are zeros)."""
    if not code:
        raise ValidationError("empty short code")
    number = 0
    for char in code:
        digit = _INDEX.get(char)
        if digit is None:
            raise ValidationError(f"{char!r} is not a base62 digit")
        number = number * BASE + digit
    return number


def code_space(length: int = CODE_LENGTH) -> int:
    """How many distinct codes of this length exist: 62^7 = 3,521,614,606,208."""
    if length <= 0:
        raise ValidationError("code length must be positive")
    return BASE**length


# --8<-- [end:codec]


# --8<-- [start:scramble]
HALF_BITS = 20  # two 20-bit halves = a 40-bit domain, ~1.1 x 10^12 counters, always 7 chars
ROUNDS = 4
_HALF_MASK = (1 << HALF_BITS) - 1


def _round_function(value: int, round_no: int, key: int) -> int:
    """Any deterministic function works -- Feistel stays bijective whatever this returns."""
    seed = f"{key}:{round_no}:{value}".encode()
    return int.from_bytes(hashlib.blake2b(seed, digest_size=8).digest(), "big") & _HALF_MASK


def scramble(counter: int, key: int = 0x5EED) -> int:
    """Permute a counter over 40 bits so codes are unguessable but still collision-free.

    A Feistel network is a bijection, so distinct counters give distinct outputs: you keep the
    "no collision check needed" property of a counter and lose the "id+1 is someone else's link"
    enumeration attack.
    """
    if not 0 <= counter < 1 << (2 * HALF_BITS):
        raise ValidationError(f"counter must fit in {2 * HALF_BITS} bits")
    left, right = counter >> HALF_BITS, counter & _HALF_MASK
    for round_no in range(ROUNDS):
        left, right = right, left ^ _round_function(right, round_no, key)
    return (left << HALF_BITS) | right


def unscramble(scrambled: int, key: int = 0x5EED) -> int:
    """Recover the counter from a scrambled value: the same rounds, run backwards."""
    if not 0 <= scrambled < 1 << (2 * HALF_BITS):
        raise ValidationError(f"value must fit in {2 * HALF_BITS} bits")
    left, right = scrambled >> HALF_BITS, scrambled & _HALF_MASK
    for round_no in reversed(range(ROUNDS)):
        left, right = right ^ _round_function(left, round_no, key), left
    return (left << HALF_BITS) | right


# --8<-- [end:scramble]


# --8<-- [start:store]
class CodeStore(Protocol):
    """The single operation the write path needs: an atomic insert-if-absent."""

    def claim(self, code: str, long_url: str) -> str | None:
        """Bind ``code`` to ``long_url`` if free; otherwise return the URL already bound."""
        ...

    def resolve(self, code: str) -> str | None: ...


class InMemoryCodeStore:
    """Stand-in for the key-value store behind the shortener.

    ``claim`` is the conditional write every real store offers: ``INSERT ... ON CONFLICT DO
    NOTHING`` in Postgres, ``attribute_not_exists(code)`` in DynamoDB, ``SETNX`` in Redis.
    ``_lock`` guards ``_rows`` and makes the read-then-write pair atomic, exactly as the
    database does across concurrent writers.
    """

    def __init__(self) -> None:
        self._rows: dict[str, str] = {}
        self._lock = threading.Lock()

    def claim(self, code: str, long_url: str) -> str | None:
        with self._lock:
            existing = self._rows.get(code)
            if existing is None:
                self._rows[code] = long_url
                return None
            return existing

    def resolve(self, code: str) -> str | None:
        with self._lock:
            return self._rows.get(code)

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)


# --8<-- [end:store]


# --8<-- [start:strategies]
@dataclass(frozen=True, slots=True)
class ShortLink:
    code: str
    long_url: str
    attempts: int  # 1 unless the candidate code was taken and had to be retried


class ShortCodeStrategy(ABC):
    """Propose a candidate code, claim it conditionally, retry only on a real collision.

    Returning early when the taken code already points at the same URL makes ``shorten``
    idempotent for hash codes and costs nothing for the id-based strategies, whose candidates
    never repeat.
    """

    max_attempts = 8

    def __init__(self, store: CodeStore) -> None:
        self._store = store
        self._stats_lock = threading.Lock()
        self._collisions = 0

    @property
    def collisions(self) -> int:
        with self._stats_lock:
            return self._collisions

    def shorten(self, long_url: str) -> ShortLink:
        if not long_url.strip():
            raise ValidationError("long_url must not be empty")
        for attempt in range(self.max_attempts):
            code = self.candidate(long_url, attempt)
            owner = self._store.claim(code, long_url)
            if owner is None or owner == long_url:
                return ShortLink(code, long_url, attempt + 1)
            with self._stats_lock:
                self._collisions += 1
        raise ConflictError(f"no free code for {long_url!r} after {self.max_attempts} attempts")

    @abstractmethod
    def candidate(self, long_url: str, attempt: int) -> str:
        """The code to try on this attempt; ``attempt`` is 0 on the first try."""


class CounterCodes(ShortCodeStrategy):
    """Monotonic counter, permuted, then base62: seven characters, never a collision.

    ``_counter_lock`` guards ``_next``. In production the counter is not process-local: each
    node leases a range (say a million ids) from a single-leader store and serves it from
    memory, so the coordination cost is one round trip per million links.
    """

    def __init__(self, store: CodeStore, start: int = 1, key: int = 0x5EED) -> None:
        super().__init__(store)
        self._next = start
        self._key = key
        self._counter_lock = threading.Lock()

    def candidate(self, long_url: str, attempt: int) -> str:
        with self._counter_lock:
            value = self._next
            self._next += 1
        return encode(scramble(value, self._key), CODE_LENGTH)


class SnowflakeCodes(ShortCodeStrategy):
    """A Snowflake id in base62: no shared counter, at the price of a longer code.

    The id carries 41 bits of timestamp plus machine and sequence bits, so the number is much
    larger than a counter that started at 1 -- around 10 characters instead of 7.
    """

    def __init__(self, store: CodeStore, machine_id: int = 1, clock: Clock | None = None) -> None:
        super().__init__(store)
        self._ids = SnowflakeGenerator(machine_id=machine_id, clock=clock)

    def candidate(self, long_url: str, attempt: int) -> str:
        return encode(self._ids.next_id())


class HashCodes(ShortCodeStrategy):
    """First ``length`` base62 digits of a hash of the URL; salt and retry when taken.

    Identical URLs collapse to one row for free, and no node needs to know what any other node
    is doing. The cost is a real collision rate: with 10^11 links in a 3.5 x 10^12 code space
    roughly 3% of writes need a second attempt.
    """

    def __init__(self, store: CodeStore, length: int = CODE_LENGTH) -> None:
        super().__init__(store)
        self._length = length

    def candidate(self, long_url: str, attempt: int) -> str:
        seed = long_url if attempt == 0 else f"{long_url}#{attempt}"
        digest = hashlib.sha256(seed.encode()).digest()
        return encode(int.from_bytes(digest, "big") % code_space(self._length), self._length)


# --8<-- [end:strategies]


def main() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from common import FakeClock

    years = code_space() // (100_000_000 * 365)
    print(f"code space: 62^{CODE_LENGTH} = {code_space():,} codes = ~{years} years at 100M/day")

    print("base62 round trip:")
    for number in (0, 61, 62, code_space() - 1):
        code = encode(number, CODE_LENGTH)
        print(f"  {number:>19,} -> {code} -> {decode(code):,}")

    store = InMemoryCodeStore()
    counter = CounterCodes(store, start=1)
    print("counter codes: consecutive counters, unrelated codes (Feistel permutation)")
    for value in (1, 2, 3):
        link = counter.shorten("https://example.com/docs/getting-started")
        print(f"  counter {value} -> {link.code}   (attempts={link.attempts})")
    print(f"  the permutation is reversible: unscramble(scramble(7)) = {unscramble(scramble(7))}")

    clock = FakeClock(start=1_750_000_000.0)
    flake = SnowflakeCodes(store, machine_id=3, clock=clock)
    codes = [flake.shorten(f"https://example.com/p/{i}").code for i in range(3)]
    print(f"snowflake codes: {codes} -- {len(codes[0])} chars, sorted={codes == sorted(codes)}")

    hashes = HashCodes(store)
    first = hashes.shorten("https://example.com/pricing")
    again = hashes.shorten("https://example.com/pricing")
    print(f"hash codes deduplicate: {first.code} == {again.code} in {again.attempts} attempt")

    crowded = HashCodes(InMemoryCodeStore(), length=1)  # 62 codes: collisions arrive fast
    links = [crowded.shorten(f"https://example.com/x/{i}") for i in range(30)]
    retried = sum(1 for link in links if link.attempts > 1)
    worst = max(link.attempts for link in links)
    print(
        f"1-char code space (62 codes), 30 links: {retried} needed a retry, "
        f"{crowded.collisions} collisions, worst case {worst} attempts"
    )

    pool_store = InMemoryCodeStore()
    threaded = CounterCodes(pool_store, start=1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: threaded.shorten(f"https://x.test/{i}").code, range(800)))
    print(f"8 threads x 100 links: {len(set(results))} distinct codes, {len(pool_store)} rows")


if __name__ == "__main__":
    main()
