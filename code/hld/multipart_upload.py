"""Object storage: multipart upload with ETags, versioned metadata, and XOR erasure coding.

What the module demonstrates, in the order an interviewer asks about it:

* ``ObjectStore`` is the **metadata service**: buckets, objects, versions and in-flight uploads.
  ``initiate_upload`` / ``upload_part`` / ``complete_upload`` are S3's three calls; a part can be
  re-uploaded at any time (the newest wins), parts may arrive in any order and from any machine,
  and ``complete_upload`` verifies every ETag before the object becomes visible.
* The object's ETag is S3's: the MD5 of one part, or ``md5(concatenated part digests)-<count>``
  for a multipart object, which is why a multipart ETag is not the MD5 of the file.
* ``ErasureVolume`` is the **data service**: every blob is split into ``k`` data blocks plus one
  XOR parity block on ``k + 1`` nodes, so a lost block is reconstructed, bit rot is caught by
  per-block checksums during ``scrub``, and the storage overhead is 1.33x rather than 3x.
* Deletes are versions (delete markers), and ``collect_garbage`` reclaims abandoned uploads and
  blobs that no version references any more.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import reduce

from common import (
    Clock,
    ConflictError,
    FakeClock,
    IdGenerator,
    InvalidStateError,
    NotFoundError,
    SequentialIdGenerator,
    SystemClock,
    ValidationError,
)

MIN_PART_BYTES = 5 * 1024 * 1024  # S3's minimum for every part except the last


def content_etag(data: bytes) -> str:
    """MD5 of the bytes: the ETag of a single-part object, and the digest of one part."""
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


# --8<-- [start:multipart]
@dataclass(frozen=True, slots=True)
class Part:
    number: int
    etag: str
    size: int


@dataclass(slots=True)
class UploadSession:
    """One in-flight multipart upload. Parts land independently, in any order."""

    upload_id: str
    bucket: str
    key: str
    started_at: float
    parts: dict[int, Part] = field(default_factory=dict)
    blobs: dict[int, str] = field(default_factory=dict)  # part number -> blob id in the data service


@dataclass(frozen=True, slots=True)
class ObjectVersion:
    key: str
    version_id: str
    etag: str
    size: int
    created_at: float
    blob_id: str | None  # None for a delete marker

    @property
    def delete_marker(self) -> bool:
        return self.blob_id is None


def multipart_etag(part_etags: Sequence[str]) -> str:
    """S3's multipart ETag: md5 of the concatenated part digests, then '-<part count>'.

    Clients that compare this against the md5 of the whole file are the single most common
    source of "the upload is corrupt" support tickets: it never matches, by construction.
    """
    if not part_etags:
        raise ValidationError("a multipart object needs at least one part")
    digest = hashlib.md5(usedforsecurity=False)
    for etag in part_etags:
        digest.update(bytes.fromhex(etag))
    return f"{digest.hexdigest()}-{len(part_etags)}"


# --8<-- [end:multipart]


# --8<-- [start:erasure]
def xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right, strict=True))


def parity_of(blocks: Sequence[bytes]) -> bytes:
    """XOR parity: the (k, k+1) erasure code. Any one missing block is the XOR of the rest."""
    if not blocks:
        raise ValidationError("parity needs at least one block")
    return reduce(xor_bytes, blocks)


def split_blocks(data: bytes, count: int) -> list[bytes]:
    """Split into ``count`` equal blocks, zero-padding the last one so XOR is well defined."""
    if count <= 0:
        raise ValidationError("count must be positive")
    size = -(-len(data) // count) or 1  # ceiling division, never zero
    padded = data.ljust(size * count, b"\0")
    return [padded[i * size : (i + 1) * size] for i in range(count)]


class ErasureVolume:
    """The data service: k data blocks plus one XOR parity block, one per node.

    ``_lock`` guards the block table and the per-block checksums. A read reconstructs a block
    that is missing or whose checksum does not match; two failures in one stripe are
    unrecoverable, which is exactly the durability trade (k, k+1) buys: 1/k extra storage
    instead of 2x, at the price of tolerating one failure per stripe rather than two.
    """

    def __init__(self, nodes: Sequence[str], data_blocks: int = 3) -> None:
        if data_blocks < 1:
            raise ValidationError("data_blocks must be positive")
        if len(set(nodes)) != len(nodes) or len(nodes) < data_blocks + 1:
            raise ValidationError(f"need {data_blocks + 1} distinct nodes for a {data_blocks}+1 code")
        self._nodes = list(nodes)
        self._k = data_blocks
        self._blocks: dict[str, list[bytes | None]] = {}
        self._checksums: dict[str, list[str]] = {}
        self._sizes: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def data_blocks(self) -> int:
        return self._k

    def placement(self, blob_id: str) -> list[str]:
        """Which nodes hold this blob's k+1 blocks; a real system spreads them across racks."""
        start = int(hashlib.md5(blob_id.encode(), usedforsecurity=False).hexdigest()[:8], 16)
        return [self._nodes[(start + i) % len(self._nodes)] for i in range(self._k + 1)]

    def write(self, blob_id: str, data: bytes) -> list[str]:
        blocks = split_blocks(data, self._k)
        stripe: list[bytes | None] = [*blocks, parity_of(blocks)]
        with self._lock:
            self._blocks[blob_id] = stripe
            self._checksums[blob_id] = [content_etag(b or b"") for b in stripe]
            self._sizes[blob_id] = len(data)
        return self.placement(blob_id)

    def read(self, blob_id: str) -> bytes:
        """Rebuild the object, reconstructing at most one damaged or missing block."""
        with self._lock:
            stripe, size = self._stripe(blob_id), self._sizes[blob_id]
            healthy = [
                block if block is not None and content_etag(block) == checksum else None
                for block, checksum in zip(stripe, self._checksums[blob_id], strict=True)
            ]
        missing = [i for i, block in enumerate(healthy) if block is None]
        if len(missing) > 1:
            raise InvalidStateError(f"{blob_id}: {len(missing)} blocks lost, only one is recoverable")
        if missing:
            survivors = [block for block in healthy if block is not None]
            healthy[missing[0]] = parity_of(survivors)  # XOR is its own inverse
        return b"".join(block or b"" for block in healthy[: self._k])[:size]

    def scrub(self) -> list[str]:
        """Background verification: rewrite blocks whose checksum no longer matches the data."""
        repaired: list[str] = []
        for blob_id in list(self._blocks):
            with self._lock:
                stripe, checksums = self._stripe(blob_id), self._checksums[blob_id]
                damaged = [
                    i
                    for i, (block, checksum) in enumerate(zip(stripe, checksums, strict=True))
                    if block is None or content_etag(block) != checksum
                ]
            if not damaged:
                continue
            data = self.read(blob_id)  # raises when the stripe is beyond repair
            self.write(blob_id, data)
            repaired.append(blob_id)
        return repaired

    def damage(self, blob_id: str, index: int, *, lose: bool = False) -> None:
        """Simulate a dead disk (``lose``) or silent bit rot in one block."""
        with self._lock:
            stripe = self._stripe(blob_id)
            if not 0 <= index < len(stripe):
                raise ValidationError(f"block {index} is outside the stripe")
            block = stripe[index]
            stripe[index] = None if lose or block is None else bytes(len(block))

    def delete(self, blob_id: str) -> None:
        with self._lock:
            self._blocks.pop(blob_id, None)
            self._checksums.pop(blob_id, None)
            self._sizes.pop(blob_id, None)

    def stored_bytes(self) -> int:
        """Physical bytes on disk, so the demo can compare 1.33x coding with 3x replication."""
        with self._lock:
            return sum(len(block or b"") for stripe in self._blocks.values() for block in stripe)

    def logical_bytes(self) -> int:
        with self._lock:
            return sum(self._sizes.values())

    def blob_ids(self) -> list[str]:
        with self._lock:
            return list(self._blocks)

    def _stripe(self, blob_id: str) -> list[bytes | None]:
        """Caller holds ``_lock``."""
        if blob_id not in self._blocks:
            raise NotFoundError(f"unknown blob {blob_id!r}")
        return self._blocks[blob_id]


# --8<-- [end:erasure]


# --8<-- [start:store]
class ObjectStore:
    """The metadata service in front of the data service.

    ``_lock`` guards the bucket set, the version lists and the upload table. Metadata is tiny
    and transactional; the bytes live in ``ErasureVolume`` and are only ever referenced by
    ``blob_id``, which is why a metadata partition can be a database and the data plane cannot.
    """

    def __init__(
        self,
        volume: ErasureVolume,
        *,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        min_part_bytes: int = MIN_PART_BYTES,
    ) -> None:
        self._volume = volume
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("id")
        self._min_part = min_part_bytes
        self._buckets: set[str] = set()
        self._versions: dict[tuple[str, str], list[ObjectVersion]] = {}
        self._uploads: dict[str, UploadSession] = {}
        self._lock = threading.Lock()

    def create_bucket(self, bucket: str) -> None:
        with self._lock:
            if bucket in self._buckets:
                raise ConflictError(f"bucket {bucket!r} exists")
            self._buckets.add(bucket)

    # -- multipart ---------------------------------------------------------------------
    def initiate_upload(self, bucket: str, key: str) -> str:
        with self._lock:
            self._require_bucket(bucket)
            upload_id = self._ids.next_id()
            self._uploads[upload_id] = UploadSession(upload_id, bucket, key, self._clock.now())
            return upload_id

    def upload_part(self, upload_id: str, part_number: int, data: bytes) -> str:
        """Store one part and return its ETag. Re-uploading a part number replaces it."""
        if not 1 <= part_number <= 10_000:
            raise ValidationError("part numbers run from 1 to 10000")
        blob_id = f"part-{self._ids.next_id()}"
        self._volume.write(blob_id, data)
        with self._lock:
            session = self._session(upload_id)
            stale = session.blobs.get(part_number)
            session.parts[part_number] = Part(part_number, content_etag(data), len(data))
            session.blobs[part_number] = blob_id
        if stale is not None:
            self._volume.delete(stale)  # the retry's bytes replace the first attempt's
        return session.parts[part_number].etag

    def list_parts(self, upload_id: str) -> list[Part]:
        with self._lock:
            return [self._session(upload_id).parts[n] for n in sorted(self._session(upload_id).parts)]

    def complete_upload(self, upload_id: str, parts: Sequence[tuple[int, str]]) -> ObjectVersion:
        """Verify every ETag, concatenate the parts and publish the object atomically."""
        with self._lock:
            session = self._session(upload_id)
            numbers = [number for number, _ in parts]
            if numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
                raise ValidationError("parts must be listed once each, in ascending order")
            for number, etag in parts:
                held = session.parts.get(number)
                if held is None:
                    raise NotFoundError(f"part {number} was never uploaded")
                if held.etag != etag:
                    raise ConflictError(f"part {number} etag mismatch: the part changed under you")
            for number, _ in parts[:-1]:
                if session.parts[number].size < self._min_part:
                    raise ValidationError(f"part {number} is below the {self._min_part} byte minimum")
            blobs = [session.blobs[number] for number, _ in parts]
            etags = [session.parts[number].etag for number, _ in parts]
            orphans = [b for n, b in session.blobs.items() if n not in set(numbers)]
        data = b"".join(self._volume.read(blob) for blob in blobs)
        blob_id = f"obj-{self._ids.next_id()}"
        self._volume.write(blob_id, data)
        version = ObjectVersion(
            session.key, self._ids.next_id(), multipart_etag(etags), len(data), self._clock.now(), blob_id
        )
        with self._lock:
            self._versions.setdefault((session.bucket, session.key), []).append(version)
            del self._uploads[upload_id]
        for blob in [*blobs, *orphans]:
            self._volume.delete(blob)  # the parts are now redundant copies of the object
        return version

    def abort_upload(self, upload_id: str) -> int:
        """Discard an upload and free its parts; returns how many parts were reclaimed."""
        with self._lock:
            session = self._session(upload_id)
            blobs = list(session.blobs.values())
            del self._uploads[upload_id]
        for blob in blobs:
            self._volume.delete(blob)
        return len(blobs)

    # -- objects -----------------------------------------------------------------------
    def put_object(self, bucket: str, key: str, data: bytes) -> ObjectVersion:
        blob_id = f"obj-{self._ids.next_id()}"
        self._volume.write(blob_id, data)
        version = ObjectVersion(
            key, self._ids.next_id(), content_etag(data), len(data), self._clock.now(), blob_id
        )
        with self._lock:
            self._require_bucket(bucket)
            self._versions.setdefault((bucket, key), []).append(version)
        return version

    def get_object(self, bucket: str, key: str, version_id: str | None = None) -> bytes:
        with self._lock:
            versions = self._versions.get((bucket, key), [])
            if version_id is None:
                current = versions[-1] if versions else None
            else:
                current = next((v for v in versions if v.version_id == version_id), None)
            if current is None or current.delete_marker:
                raise NotFoundError(f"{bucket}/{key} not found")
            blob_id = current.blob_id
        return self._volume.read(blob_id or "")

    def delete_object(self, bucket: str, key: str) -> ObjectVersion:
        """A delete is a new version: the older ones stay readable by version id."""
        marker = ObjectVersion(key, self._ids.next_id(), "", 0, self._clock.now(), None)
        with self._lock:
            if (bucket, key) not in self._versions:
                raise NotFoundError(f"{bucket}/{key} not found")
            self._versions[(bucket, key)].append(marker)
        return marker

    def versions(self, bucket: str, key: str) -> list[ObjectVersion]:
        with self._lock:
            return list(self._versions.get((bucket, key), []))

    def list_objects(
        self, bucket: str, prefix: str = "", limit: int = 1_000, after: str | None = None
    ) -> tuple[list[str], str | None]:
        """Keys in lexicographic order with a continuation token: the flat namespace S3 exposes."""
        if limit <= 0:
            raise ValidationError("limit must be positive")
        with self._lock:
            self._require_bucket(bucket)
            keys = sorted(
                key
                for (b, key), versions in self._versions.items()
                if b == bucket and key.startswith(prefix) and not versions[-1].delete_marker
            )
        page = [key for key in keys if after is None or key > after][:limit]
        more = len([key for key in keys if after is None or key > after]) > limit
        return page, (page[-1] if page and more else None)

    def collect_garbage(self, abandoned_after: float) -> tuple[int, int]:
        """Reclaim uploads older than ``abandoned_after`` seconds and unreferenced blobs."""
        cutoff = self._clock.now() - abandoned_after
        with self._lock:
            stale = [u for u in self._uploads.values() if u.started_at < cutoff]
        parts = sum(self.abort_upload(session.upload_id) for session in stale)
        with self._lock:
            live = {
                version.blob_id
                for versions in self._versions.values()
                for version in versions
                if version.blob_id is not None
            }
            held = {b for session in self._uploads.values() for b in session.blobs.values()}
            orphans = [b for b in self._volume.blob_ids() if b not in live and b not in held]
        for blob in orphans:
            self._volume.delete(blob)
        return len(stale), parts + len(orphans)

    def _require_bucket(self, bucket: str) -> None:
        """Caller holds ``_lock``."""
        if bucket not in self._buckets:
            raise NotFoundError(f"unknown bucket {bucket!r}")

    def _session(self, upload_id: str) -> UploadSession:
        """Caller holds ``_lock``."""
        if upload_id not in self._uploads:
            raise NotFoundError(f"unknown upload {upload_id!r}")
        return self._uploads[upload_id]


# --8<-- [end:store]


def main() -> None:
    clock = FakeClock(start=1_700_000_000.0)
    volume = ErasureVolume(["n1", "n2", "n3", "n4", "n5"], data_blocks=3)
    store = ObjectStore(volume, clock=clock, ids=SequentialIdGenerator("id"), min_part_bytes=8)
    store.create_bucket("videos")

    upload = store.initiate_upload("videos", "clip.mp4")
    chunks = [b"the first chunk.", b"the second chunk", b"tail"]
    etags = [store.upload_part(upload, i + 1, chunk) for i, chunk in enumerate(chunks)]
    print(f"initiate + 3 parts    : upload {upload}, etags {[e[:8] for e in etags]}")
    retry = store.upload_part(upload, 2, chunks[1])
    print(f"part 2 re-uploaded    : same bytes -> same etag {retry == etags[1]}, {len(store.list_parts(upload))} parts held")
    try:
        store.complete_upload(upload, [(1, etags[0]), (2, "0" * 32), (3, etags[2])])
    except ConflictError as exc:
        print(f"complete, bad etag    : rejected: {exc}")
    version = store.complete_upload(upload, list(enumerate(etags, start=1)))
    print(f"complete              : etag {version.etag} ({version.size} bytes, version {version.version_id})")
    print(f"get                   : {store.get_object('videos', 'clip.mp4')!r}")

    store.put_object("videos", "clip.mp4", b"a whole new take of the clip")
    store.delete_object("videos", "clip.mp4")
    history = store.versions("videos", "clip.mp4")
    print(
        f"put v2 then delete    : {len(history)} versions, newest is a delete marker "
        f"{history[-1].delete_marker}, listing {store.list_objects('videos')[0]}"
    )
    print(f"read the first version: {store.get_object('videos', 'clip.mp4', version.version_id)!r}")

    blob = history[1].blob_id or ""
    second = history[1].version_id
    overhead = volume.stored_bytes() / volume.logical_bytes()
    print(f"placement             : {blob} on {volume.placement(blob)}, 3 data + 1 parity")
    print(f"storage overhead      : {overhead:.2f}x with 3+1 coding, against 3.00x for three copies")
    volume.damage(blob, 1, lose=True)
    print(f"one block lost        : read still returns {store.get_object('videos', 'clip.mp4', second)!r}")
    print(f"scrub                 : rebuilt {volume.scrub()} from parity")
    volume.damage(blob, 0)  # silent bit rot: the bytes changed, the checksum did not
    print(f"bit rot in a block    : checksum mismatch, scrub rebuilt {volume.scrub()}")
    volume.damage(blob, 0, lose=True)
    volume.damage(blob, 2, lose=True)
    try:
        store.get_object("videos", "clip.mp4", second)
    except InvalidStateError as exc:
        print(f"two blocks lost       : {exc}")

    for name in ("never-finished.mp4", "also-abandoned.mp4"):
        abandoned = store.initiate_upload("videos", name)
        store.upload_part(abandoned, 1, b"orphaned bytes")
    clock.advance(8 * 86_400)
    uploads, blobs = store.collect_garbage(abandoned_after=7 * 86_400)
    print(f"garbage collection    : {uploads} abandoned uploads aborted, {blobs} blobs reclaimed")


if __name__ == "__main__":
    main()
