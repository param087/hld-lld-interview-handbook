import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, NotFoundError, ValidationError
from hld.chunker_delta_sync import (
    MAX_CHUNK,
    MIN_CHUNK,
    Chunk,
    ChunkStore,
    MetadataStore,
    RollingHash,
    chunk_hash,
    compute_delta,
    content_defined_chunks,
    fixed_chunks,
)

DATA = random.Random(42).randbytes(64 * 1024)


def test_rolling_hash_equals_the_window_hashed_from_scratch() -> None:
    window = 8
    rolling = RollingHash(window)
    payload = b"the quick brown fox jumps over the lazy dog"
    for index, byte in enumerate(payload):
        value = rolling.push(byte)
        tail = payload[max(0, index + 1 - window) : index + 1]
        expected = 0
        for b in tail:
            expected = (expected * RollingHash.BASE + b) % RollingHash.MOD
        assert value == expected


def test_chunks_tile_the_file_exactly_and_respect_the_size_bounds() -> None:
    chunks = content_defined_chunks(DATA)
    assert chunks[0].offset == 0
    assert sum(c.length for c in chunks) == len(DATA)
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert previous.offset + previous.length == current.offset
    for chunk in chunks[:-1]:  # the trailing remainder may be shorter than MIN_CHUNK
        assert MIN_CHUNK <= chunk.length <= MAX_CHUNK
    for chunk in chunks:
        assert chunk.hash == chunk_hash(DATA[chunk.offset : chunk.offset + chunk.length])


def test_an_insert_at_the_front_reshuffles_fixed_blocks_but_not_content_defined_ones() -> None:
    """The whole reason content-defined chunking exists."""
    edited = b"\x00" + DATA
    base = content_defined_chunks(DATA)
    cdc = compute_delta(content_defined_chunks(edited), frozenset(c.hash for c in base))
    fixed = compute_delta(fixed_chunks(edited), frozenset(c.hash for c in fixed_chunks(DATA)))
    assert len(cdc.upload) == 1  # one boundary shifts, the rest re-synchronise
    assert len(cdc.reuse) == len(base) - 1
    assert fixed.reuse == ()  # every fixed block shifted by one byte and so changed
    assert cdc.savings > 0.95
    assert fixed.savings == 0.0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d[:30_000] + b"replacement" + d[30_011:],
        lambda d: d + b"appended tail",
        lambda d: d[:100] + d[200:],
    ],
)
def test_local_edits_only_reupload_a_few_chunks(mutate) -> None:  # type: ignore[no-untyped-def]
    base = content_defined_chunks(DATA)
    delta = compute_delta(content_defined_chunks(mutate(DATA)), frozenset(c.hash for c in base))
    assert 1 <= len(delta.upload) <= 3
    assert delta.savings > 0.9
    assert delta.bytes_total == sum(c.length for c in content_defined_chunks(mutate(DATA)))


def test_the_chunk_store_deduplicates_and_reference_counts() -> None:
    store = ChunkStore()
    digest, is_new = store.put(b"identical block")
    assert is_new
    again, is_new_again = store.put(b"identical block")
    assert (again, is_new_again) == (digest, False)
    assert store.stats == (1, 1, len(b"identical block"))
    store.release(digest)
    assert store.get(digest) == b"identical block"  # one reference is still held
    store.release(digest)
    with pytest.raises(NotFoundError):
        store.get(digest)


def test_commit_is_a_compare_and_set_and_the_loser_keeps_a_conflicted_copy() -> None:
    metadata = MetadataStore()
    alice = content_defined_chunks(DATA)
    bob = content_defined_chunks(DATA + b"bob was here")
    v1 = metadata.commit("report.pdf", 0, alice, "alice")
    assert v1.version == 1
    metadata.commit("report.pdf", 1, bob, "alice")
    with pytest.raises(ConflictError, match="moved to v2"):
        metadata.commit("report.pdf", 1, bob, "bob")
    copy = metadata.conflicted_copy("report.pdf", bob, "bob")
    assert copy.file_id == "report.pdf (bob's conflicted copy)"
    assert metadata.head("report.pdf") is not None
    assert metadata.head("report.pdf").version == 2  # type: ignore[union-attr]
    assert metadata.head("missing.pdf") is None


def test_known_hashes_lets_a_client_skip_what_the_server_already_has() -> None:
    metadata = MetadataStore()
    chunks = content_defined_chunks(DATA)
    metadata.commit("a.bin", 0, chunks, "alice")
    delta = compute_delta(chunks, metadata.known_hashes())
    assert delta.upload == ()  # a re-upload of an unchanged file transfers nothing
    assert delta.savings == 1.0


@pytest.mark.parametrize(
    "call",
    [
        lambda: RollingHash(0),
        lambda: content_defined_chunks(b"x", avg_size=1000),
        lambda: content_defined_chunks(b"x", min_size=8192, avg_size=1024),
        lambda: fixed_chunks(b"x", size=0),
    ],
)
def test_bad_parameters_are_rejected(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        call()


def test_concurrent_commits_from_one_base_elect_exactly_one_winner() -> None:
    metadata = MetadataStore()
    chunks = [Chunk(chunk_hash(b"seed"), 0, 4)]
    metadata.commit("shared.doc", 0, chunks, "seed")
    outcomes: list[str] = []

    def commit(index: int) -> str:
        try:
            metadata.commit("shared.doc", 1, [Chunk(chunk_hash(str(index).encode()), 0, 4)], f"u{index}")
        except ConflictError:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(commit, range(32)))
    assert outcomes.count("committed") == 1
    assert outcomes.count("conflict") == 31
    assert metadata.head("shared.doc").version == 2  # type: ignore[union-attr]
