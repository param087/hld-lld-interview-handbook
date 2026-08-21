from concurrent.futures import ThreadPoolExecutor

import pytest

from common import (
    ConflictError,
    FakeClock,
    InvalidStateError,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)
from hld.multipart_upload import (
    ErasureVolume,
    ObjectStore,
    content_etag,
    multipart_etag,
    parity_of,
    split_blocks,
)

NODES = ["n1", "n2", "n3", "n4", "n5"]


def store(min_part_bytes: int = 8) -> tuple[ObjectStore, ErasureVolume]:
    volume = ErasureVolume(NODES, data_blocks=3)
    objects = ObjectStore(
        volume,
        clock=FakeClock(start=1_000.0),
        ids=SequentialIdGenerator("id"),
        min_part_bytes=min_part_bytes,
    )
    objects.create_bucket("videos")
    return objects, volume


def test_multipart_upload_completes_with_the_s3_style_etag() -> None:
    objects, _ = store()
    upload = objects.initiate_upload("videos", "clip.mp4")
    chunks = [b"first part bytes", b"second part byte", b"tail"]
    etags = [objects.upload_part(upload, i, chunk) for i, chunk in enumerate(chunks, start=1)]
    assert etags == [content_etag(chunk) for chunk in chunks]

    version = objects.complete_upload(upload, list(enumerate(etags, start=1)))
    assert version.etag == multipart_etag(etags) and version.etag.endswith("-3")
    assert version.etag != content_etag(b"".join(chunks)), "a multipart etag is not the file's md5"
    assert version.size == sum(len(c) for c in chunks)
    assert objects.get_object("videos", "clip.mp4") == b"".join(chunks)
    with pytest.raises(NotFoundError):
        objects.list_parts(upload)  # the session is gone once the object is published


def test_parts_may_arrive_out_of_order_and_a_retry_replaces_the_earlier_attempt() -> None:
    objects, _ = store()
    upload = objects.initiate_upload("videos", "clip.mp4")
    objects.upload_part(upload, 3, b"third and last")
    objects.upload_part(upload, 1, b"the wrong bytes.")
    objects.upload_part(upload, 2, b"second part byte")
    good = objects.upload_part(upload, 1, b"first part bytes")  # the retry wins
    assert [p.number for p in objects.list_parts(upload)] == [1, 2, 3]

    etags = [p.etag for p in objects.list_parts(upload)]
    assert etags[0] == good
    version = objects.complete_upload(upload, list(enumerate(etags, start=1)))
    assert objects.get_object("videos", "clip.mp4") == b"first part bytessecond part bytethird and last"
    assert version.size == 46


def test_complete_upload_validates_etags_order_and_part_size() -> None:
    objects, _ = store(min_part_bytes=16)
    upload = objects.initiate_upload("videos", "clip.mp4")
    etags = [objects.upload_part(upload, i, data) for i, data in ((1, b"tiny"), (2, b"also small"))]
    with pytest.raises(ConflictError):
        objects.complete_upload(upload, [(1, etags[0]), (2, "f" * 32)])
    with pytest.raises(NotFoundError):
        objects.complete_upload(upload, [(1, etags[0]), (9, etags[1])])
    with pytest.raises(ValidationError):
        objects.complete_upload(upload, [(2, etags[1]), (1, etags[0])])  # descending
    with pytest.raises(ValidationError):
        objects.complete_upload(upload, list(enumerate(etags, start=1)))  # part 1 below the minimum


def test_versioning_keeps_history_and_a_delete_is_a_marker() -> None:
    objects, _ = store()
    first = objects.put_object("videos", "clip.mp4", b"take one")
    objects.put_object("videos", "clip.mp4", b"take two")
    assert objects.get_object("videos", "clip.mp4") == b"take two"
    assert objects.get_object("videos", "clip.mp4", first.version_id) == b"take one"

    marker = objects.delete_object("videos", "clip.mp4")
    assert marker.delete_marker and len(objects.versions("videos", "clip.mp4")) == 3
    with pytest.raises(NotFoundError):
        objects.get_object("videos", "clip.mp4")
    assert objects.get_object("videos", "clip.mp4", first.version_id) == b"take one"
    assert objects.list_objects("videos")[0] == [], "a delete marker hides the key from listings"


def test_listing_is_prefix_filtered_and_paginated_by_continuation_token() -> None:
    objects, _ = store()
    for key in ("a/1", "a/2", "a/3", "b/1"):
        objects.put_object("videos", key, b"x")
    page, token = objects.list_objects("videos", prefix="a/", limit=2)
    assert (page, token) == (["a/1", "a/2"], "a/2")
    page, token = objects.list_objects("videos", prefix="a/", limit=2, after=token)
    assert (page, token) == (["a/3"], None), "the last page carries no token"
    assert objects.list_objects("videos", limit=10)[0] == ["a/1", "a/2", "a/3", "b/1"]
    with pytest.raises(ValidationError):
        objects.list_objects("videos", limit=0)


def test_erasure_coding_survives_one_lost_block_and_catches_bit_rot() -> None:
    volume = ErasureVolume(NODES, data_blocks=3)
    payload = bytes(range(256)) * 4
    placement = volume.write("blob-1", payload)
    assert len(placement) == 4 and len(set(placement)) == 4, "one block per node"
    assert volume.read("blob-1") == payload

    volume.damage("blob-1", 1, lose=True)  # a disk dies
    assert volume.read("blob-1") == payload, "the missing block is the XOR of the others"
    assert volume.scrub() == ["blob-1"] and volume.scrub() == []

    volume.damage("blob-1", 2)  # silent corruption: the checksum no longer matches
    assert volume.read("blob-1") == payload
    assert volume.scrub() == ["blob-1"]

    volume.damage("blob-1", 0, lose=True)
    volume.damage("blob-1", 3, lose=True)
    with pytest.raises(InvalidStateError):
        volume.read("blob-1")


def test_erasure_overhead_is_one_third_not_two_extra_copies() -> None:
    volume = ErasureVolume(NODES, data_blocks=3)
    volume.write("blob-1", b"x" * 3_000)
    assert volume.logical_bytes() == 3_000
    assert volume.stored_bytes() == 4_000, "3 data blocks of 1000 B plus one parity block"
    assert volume.stored_bytes() / volume.logical_bytes() == pytest.approx(4 / 3)
    blocks = split_blocks(b"abcdef", 3)
    assert blocks == [b"ab", b"cd", b"ef"]
    assert parity_of(blocks) == bytes([ord("a") ^ ord("c") ^ ord("e"), ord("b") ^ ord("d") ^ ord("f")])
    with pytest.raises(ValidationError):
        ErasureVolume(["n1", "n2"], data_blocks=3)


def test_abandoned_uploads_and_orphan_blobs_are_collected() -> None:
    clock = FakeClock(start=1_000.0)
    volume = ErasureVolume(NODES, data_blocks=3)
    objects = ObjectStore(volume, clock=clock, ids=SequentialIdGenerator("id"), min_part_bytes=1)
    objects.create_bucket("videos")
    objects.put_object("videos", "keep.bin", b"still referenced")
    abandoned = objects.initiate_upload("videos", "gone.bin")
    objects.upload_part(abandoned, 1, b"orphan bytes")
    live = objects.initiate_upload("videos", "in-flight.bin")
    objects.upload_part(live, 1, b"still uploading")

    clock.advance(8 * 86_400)
    fresh = objects.initiate_upload("videos", "just-started.bin")
    objects.upload_part(fresh, 1, b"brand new")
    uploads, blobs = objects.collect_garbage(abandoned_after=7 * 86_400)

    assert (uploads, blobs) == (2, 2), "both uploads older than a week, one blob each"
    assert objects.get_object("videos", "keep.bin") == b"still referenced"
    assert objects.list_parts(fresh)[0].number == 1, "the fresh upload is untouched"
    with pytest.raises(NotFoundError):
        objects.list_parts(abandoned)


def test_concurrent_part_uploads_all_land_exactly_once() -> None:
    objects, volume = store(min_part_bytes=1)
    upload = objects.initiate_upload("videos", "big.bin")
    parts = 24

    def send(number: int) -> tuple[int, str]:
        return number, objects.upload_part(upload, number, f"part-{number:02d}".encode())

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(send, range(1, parts + 1)))

    held = objects.list_parts(upload)
    assert [p.number for p in held] == list(range(1, parts + 1))
    assert all(results[p.number] == p.etag for p in held)
    version = objects.complete_upload(upload, [(p.number, p.etag) for p in held])
    assert version.size == sum(p.size for p in held)
    assert objects.get_object("videos", "big.bin").startswith(b"part-01part-02")
    assert volume.blob_ids() == [version.blob_id], "the part blobs are freed once the object is written"


@pytest.mark.parametrize("part_number", [0, -1, 10_001])
def test_part_numbers_are_bounded(part_number: int) -> None:
    objects, _ = store()
    upload = objects.initiate_upload("videos", "clip.mp4")
    with pytest.raises(ValidationError):
        objects.upload_part(upload, part_number, b"data")


def test_unknown_buckets_uploads_and_blobs_are_rejected() -> None:
    objects, volume = store()
    with pytest.raises(NotFoundError):
        objects.initiate_upload("missing-bucket", "k")
    with pytest.raises(ConflictError):
        objects.create_bucket("videos")
    with pytest.raises(NotFoundError):
        objects.upload_part("no-such-upload", 1, b"data")
    with pytest.raises(NotFoundError):
        volume.read("no-such-blob")
    with pytest.raises(ValidationError):
        multipart_etag([])
