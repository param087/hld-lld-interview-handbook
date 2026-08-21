"""Content-defined chunking, content-addressed dedup and delta sync: the core of a file-sync
client in one module.

What the module demonstrates, in the order an interviewer asks about it:

* ``RollingHash`` is a Rabin-Karp hash over a sliding window: pushing one byte costs O(1)
  because the leaving byte is subtracted instead of the window being rehashed.
* ``content_defined_chunks`` cuts a file where the rolling hash matches a mask, so boundaries
  follow the *content*. Insert one byte at the front and a single chunk changes;
  ``fixed_chunks`` shows the same edit invalidating every chunk in the file.
* ``ChunkStore`` is content-addressed: the key is the chunk's SHA-256, so an identical chunk
  from any file or any user is stored once. ``put`` reports whether it was new, which is where
  the global dedup ratio comes from.
* ``compute_delta`` turns two manifests into "upload these hashes, reuse those", which is the
  number a sync client cares about: bytes on the wire, not bytes on disk.
* ``MetadataStore.commit`` is a compare-and-set on the version, so two clients editing from the
  same base cannot both win; the loser makes a conflicted copy.

Everything is deterministic and stdlib-only, so the demo runs in milliseconds.
"""

from __future__ import annotations

import hashlib
import threading
from collections import deque
from dataclasses import dataclass

from common import ConflictError, NotFoundError, ValidationError

MIN_CHUNK = 512
AVG_CHUNK = 1_024  # must be a power of two: the mask is avg - 1
MAX_CHUNK = 4_096
WINDOW = 32


# --8<-- [start:chunking]
class RollingHash:
    """Rabin-Karp over a sliding window of ``window`` bytes.

    The whole point is that ``push`` is O(1): the byte leaving the window is subtracted with a
    precomputed ``BASE^(window-1)``, so hashing a megabyte costs one multiply-add per byte
    rather than one full window hash per position.
    """

    BASE = 257
    MOD = (1 << 61) - 1  # a Mersenne prime keeps the modulo cheap and collisions rare

    def __init__(self, window: int = WINDOW) -> None:
        if window <= 0:
            raise ValidationError("window must be positive")
        self._window = window
        self._power = pow(self.BASE, window - 1, self.MOD)
        self._buffer: deque[int] = deque()
        self._value = 0

    def push(self, byte: int) -> int:
        if len(self._buffer) == self._window:
            leaving = self._buffer.popleft()
            self._value = (self._value - leaving * self._power) % self.MOD
        self._buffer.append(byte)
        self._value = (self._value * self.BASE + byte) % self.MOD
        return self._value


@dataclass(frozen=True, slots=True)
class Chunk:
    hash: str
    offset: int
    length: int


def chunk_hash(data: bytes) -> str:
    """Content address. 16 hex characters is plenty for a demo; production keeps all 64."""
    return hashlib.sha256(data).hexdigest()[:16]


def content_defined_chunks(
    data: bytes,
    min_size: int = MIN_CHUNK,
    avg_size: int = AVG_CHUNK,
    max_size: int = MAX_CHUNK,
    window: int = WINDOW,
) -> list[Chunk]:
    """Cut where the rolling hash has ``log2(avg_size)`` low zero bits.

    ``min_size`` stops pathological runs of tiny chunks and ``max_size`` bounds the worst case
    on data that never matches (an incompressible blob, or a long run of one byte). Because the
    boundary depends only on the surrounding ``window`` bytes, inserting data early in the file
    re-synchronises within one chunk instead of shifting every boundary after it.

    The realised mean lands near ``min_size + avg_size``, not ``avg_size``: no boundary can be
    taken in the first ``min_size`` bytes, so every chunk carries that floor before the
    geometric tail starts. Size the mask for the mean you actually want.
    """
    if avg_size & (avg_size - 1):
        raise ValidationError("avg_size must be a power of two")
    if not 0 < min_size <= avg_size <= max_size:
        raise ValidationError("need 0 < min_size <= avg_size <= max_size")
    mask = avg_size - 1
    hasher = RollingHash(window)
    chunks: list[Chunk] = []
    start = 0
    for index, byte in enumerate(data):
        value = hasher.push(byte)
        length = index - start + 1
        if length < min_size:
            continue
        if value & mask == 0 or length >= max_size:
            chunks.append(Chunk(chunk_hash(data[start : index + 1]), start, length))
            start = index + 1
    if start < len(data):
        chunks.append(Chunk(chunk_hash(data[start:]), start, len(data) - start))
    return chunks


def fixed_chunks(data: bytes, size: int = AVG_CHUNK) -> list[Chunk]:
    """The naive alternative, kept so the demo can show what it costs on an insert."""
    if size <= 0:
        raise ValidationError("size must be positive")
    return [
        Chunk(chunk_hash(data[offset : offset + size]), offset, len(data[offset : offset + size]))
        for offset in range(0, len(data), size)
    ]


# --8<-- [end:chunking]


# --8<-- [start:store]
class ChunkStore:
    """Content-addressed blocks with reference counts. ``_lock`` guards ``_blocks``/``_refs``.

    Storing by hash makes deduplication a side effect of addressing: the same attachment mailed
    to a thousand people is one block, and a re-upload of an unchanged file transfers nothing.
    Reference counts are what make deletion safe -- you cannot free a block another manifest
    still points at.
    """

    def __init__(self) -> None:
        self._blocks: dict[str, bytes] = {}
        self._refs: dict[str, int] = {}
        self._duplicate_puts = 0
        self._lock = threading.Lock()

    def put(self, payload: bytes) -> tuple[str, bool]:
        """``(hash, is_new)``. ``is_new`` false means the bytes never left the client."""
        digest = chunk_hash(payload)
        with self._lock:
            is_new = digest not in self._blocks
            if is_new:
                self._blocks[digest] = payload
            else:
                self._duplicate_puts += 1
            self._refs[digest] = self._refs.get(digest, 0) + 1
            return digest, is_new

    def get(self, digest: str) -> bytes:
        with self._lock:
            if digest not in self._blocks:
                raise NotFoundError(f"chunk {digest} is not in the store")
            return self._blocks[digest]

    def release(self, digest: str) -> None:
        """Drop one reference; the block is collected when the last manifest lets it go."""
        with self._lock:
            remaining = self._refs.get(digest, 0) - 1
            if remaining > 0:
                self._refs[digest] = remaining
            else:
                self._refs.pop(digest, None)
                self._blocks.pop(digest, None)

    @property
    def stats(self) -> tuple[int, int, int]:
        """``(unique blocks, duplicate puts, bytes stored)``."""
        with self._lock:
            return len(self._blocks), self._duplicate_puts, sum(map(len, self._blocks.values()))


# --8<-- [end:store]


# --8<-- [start:delta]
@dataclass(frozen=True, slots=True)
class Delta:
    upload: tuple[str, ...]  # chunk hashes the server does not have yet
    reuse: tuple[str, ...]  # chunk hashes already on the server
    bytes_upload: int
    bytes_total: int

    @property
    def savings(self) -> float:
        """Fraction of the file the client did not have to send."""
        return 1.0 - self.bytes_upload / self.bytes_total if self.bytes_total else 0.0


def compute_delta(new_chunks: list[Chunk], known: frozenset[str]) -> Delta:
    """What a client sends after the server answers "which of these hashes do you have?".

    One round trip of hashes (16 B each) decides the transfer, so a 1 GB file whose middle
    paragraph changed costs a few kilobytes of hashes plus one chunk.
    """
    upload = tuple(c.hash for c in new_chunks if c.hash not in known)
    reuse = tuple(c.hash for c in new_chunks if c.hash in known)
    by_hash = {c.hash: c.length for c in new_chunks}
    return Delta(
        upload,
        reuse,
        sum(by_hash[h] for h in upload),
        sum(c.length for c in new_chunks),
    )


# --8<-- [end:delta]


# --8<-- [start:metadata]
@dataclass(frozen=True, slots=True)
class FileVersion:
    file_id: str
    version: int
    chunks: tuple[str, ...]
    actor: str
    size: int


class MetadataStore:
    """Versioned manifests with compare-and-set commits. ``_lock`` guards ``_history``.

    This is the one component that must be strongly consistent. Chunks are immutable blobs and
    can live in an eventually consistent object store, but "which chunks are the current
    version of this file" is a single value that two clients race for, and the loser has to
    find out. Optimistic concurrency on an integer version is enough: no locks are held across
    a user's editing session, and the conflict surfaces at commit time.
    """

    def __init__(self) -> None:
        self._history: dict[str, list[FileVersion]] = {}
        self._lock = threading.Lock()

    def head(self, file_id: str) -> FileVersion | None:
        with self._lock:
            history = self._history.get(file_id)
            return history[-1] if history else None

    def commit(self, file_id: str, base_version: int, chunks: list[Chunk], actor: str) -> FileVersion:
        """Advance the file to ``base_version + 1``, or raise ``ConflictError``."""
        with self._lock:
            history = self._history.setdefault(file_id, [])
            current = history[-1].version if history else 0
            if base_version != current:
                raise ConflictError(
                    f"{file_id} moved to v{current} while {actor} edited v{base_version}"
                )
            version = FileVersion(
                file_id,
                current + 1,
                tuple(c.hash for c in chunks),
                actor,
                sum(c.length for c in chunks),
            )
            history.append(version)
            return version

    def conflicted_copy(self, file_id: str, chunks: list[Chunk], actor: str) -> FileVersion:
        """The losing client keeps its work as a new file rather than losing or merging it."""
        return self.commit(f"{file_id} ({actor}'s conflicted copy)", 0, chunks, actor)

    def known_hashes(self) -> frozenset[str]:
        with self._lock:
            return frozenset(h for history in self._history.values() for v in history for h in v.chunks)


# --8<-- [end:metadata]


def main() -> None:
    import random

    rng = random.Random(42)
    original = rng.randbytes(64 * 1024)
    base = content_defined_chunks(original)
    store = ChunkStore()
    for chunk in base:
        store.put(original[chunk.offset : chunk.offset + chunk.length])
    metadata = MetadataStore()
    v1 = metadata.commit("report.pdf", 0, base, "alice")
    average = sum(c.length for c in base) // len(base)
    print(f"report.pdf v{v1.version}: {len(original)} bytes -> {len(base)} chunks (avg {average} B)")

    edits = {
        "insert 1 byte at offset 0": b"\x00" + original,
        "edit 100 bytes in the middle": original[:30_000] + rng.randbytes(100) + original[30_100:],
        "append 4 KB at the end": original + rng.randbytes(4 * 1024),
    }
    known = frozenset(c.hash for c in base)
    fixed_known = frozenset(c.hash for c in fixed_chunks(original))
    for label, edited in edits.items():
        cdc = compute_delta(content_defined_chunks(edited), known)
        fixed = compute_delta(fixed_chunks(edited), fixed_known)
        print(f"{label}:")
        print(f"  fixed 1 KB blocks: {len(fixed.upload):3d}/{len(fixed.upload) + len(fixed.reuse):3d} chunks resent, {fixed.bytes_upload:6d} B ({fixed.savings:.0%} saved)")
        print(f"  content-defined:   {len(cdc.upload):3d}/{len(cdc.upload) + len(cdc.reuse):3d} chunks resent, {cdc.bytes_upload:6d} B ({cdc.savings:.0%} saved)")

    for chunk in base[:3]:  # a second user uploads a file that shares three blocks
        store.put(original[chunk.offset : chunk.offset + chunk.length])
    unique, duplicates, stored = store.stats
    print(f"chunk store: {unique} unique blocks, {duplicates} duplicate puts deduplicated, {stored} B stored")

    alice = content_defined_chunks(edits["append 4 KB at the end"])
    bob = content_defined_chunks(edits["edit 100 bytes in the middle"])
    metadata.commit("report.pdf", v1.version, alice, "alice")
    try:
        metadata.commit("report.pdf", v1.version, bob, "bob")
    except ConflictError as exc:
        copy = metadata.conflicted_copy("report.pdf", bob, "bob")
        print(f"conflict: {exc}")
        print(f"  resolved by keeping both: '{copy.file_id}' v{copy.version} ({copy.size} B)")


if __name__ == "__main__":
    main()
