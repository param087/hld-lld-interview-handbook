from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, NotFoundError, ValidationError
from hld.mini_kafka import OffsetOutOfRangeError
from hld.partitioned_log import (
    Acks,
    NotEnoughReplicasError,
    ReplicatedPartition,
    SegmentedLog,
    record_bytes,
)


def filled_log(records: int = 300, *, segment_bytes: int = 4_096, start: float = 1_000.0) -> SegmentedLog:
    """A log with ``records`` fixed-size records, one per simulated second."""
    log = SegmentedLog("orders", 0, segment_bytes=segment_bytes, index_interval=512)
    for i in range(records):
        log.append(f"user:{i % 10}", f"order-{i:04d}", start + i)
    return log


def test_segments_roll_when_full_and_keep_offsets_contiguous() -> None:
    log = filled_log(300)
    infos = log.segments()
    assert len(infos) > 1, "300 records of ~48 B must not fit in one 4 KB segment"
    assert infos[0].base_offset == 0 and log.end_offset == 300
    for previous, current in zip(infos, infos[1:], strict=False):
        assert previous.base_offset + previous.records == current.base_offset
        assert previous.size_bytes <= 4_096
    # the index is sparse: one entry per 512 bytes, not one per record
    assert all(info.index_entries < info.records for info in infos[:-1])
    assert record_bytes("user:7", "order-0000") == 48


def test_lookup_bisects_the_index_then_scans_a_bounded_number_of_records() -> None:
    log = filled_log(300)
    base, position, scanned = log.lookup(200)
    holder = next(info for info in log.segments() if info.base_offset == base)
    assert base <= 200 < base + holder.records
    assert 0 <= position < holder.size_bytes
    # 512-byte index interval over 48-byte records: never more than 11 records to scan
    assert scanned <= 512 // record_bytes("user:7", "order-0000")
    assert log.read(200, 3)[0].offset == 200
    with pytest.raises(OffsetOutOfRangeError):
        log.lookup(log.end_offset)


def test_retention_deletes_whole_segments_and_moves_the_log_start_offset() -> None:
    log = filled_log(300, start=1_000.0)  # timestamps 1000..1299
    before = len(log.segments())
    deleted = log.expire(1_150.0)
    assert deleted > 0 and len(log.segments()) == before - deleted
    start = log.log_start_offset
    assert start == log.segments()[0].base_offset > 0, "the start jumps a whole segment at a time"
    assert log.read(start, 1)[0].offset == start
    with pytest.raises(OffsetOutOfRangeError):
        log.read(start - 1, 1)
    # the active segment is never deleted, however old it is
    remaining = len(log.segments())
    assert log.expire(9_999.0) == remaining - 1 and len(log.segments()) == 1


def test_acks_all_advances_the_high_watermark_but_acks_one_does_not() -> None:
    part = ReplicatedPartition("orders", 0, ["n1", "n2", "n3"], min_insync=2, clock=FakeClock())
    part.produce("ann", "a", acks=Acks.ALL)
    assert part.high_watermark == 1 and part.log_end_offset("n3") == 1
    part.produce("ann", "b", acks=Acks.LEADER)
    assert part.log_end_offset("n1") == 2, "the leader has it"
    assert part.high_watermark == 1, "the followers do not, so consumers must not see it"
    assert [r.value for r in part.fetch(0)] == ["a"]
    assert part.fetch(1) == []
    with pytest.raises(ValidationError):
        part.fetch(2)
    assert part.replicate() == 2, "one record each for the two followers"
    assert part.high_watermark == 2 and [r.value for r in part.fetch(0)] == ["a", "b"]


def test_isr_shrinks_on_lag_and_min_insync_replicas_refuses_acks_all() -> None:
    clock = FakeClock(start=100.0)
    part = ReplicatedPartition("orders", 0, ["n1", "n2", "n3"], min_insync=2, max_lag=10.0, clock=clock)
    part.produce("ann", "a", acks=Acks.ALL)
    clock.advance(15)
    part.replicate("n2")  # n2 keeps fetching, n3 does not
    assert part.check_isr() == ["n3"] and part.isr == ["n1", "n2"]
    part.produce("ann", "b", acks=Acks.ALL)  # two in-sync replicas still satisfy min_insync
    assert part.high_watermark == 2
    clock.advance(15)
    assert part.check_isr() == ["n2"] and part.isr == ["n1"]
    with pytest.raises(NotEnoughReplicasError):
        part.produce("ann", "c", acks=Acks.ALL)
    part.produce("ann", "c", acks=Acks.LEADER)  # acks=1 is still accepted, without the guarantee
    assert part.log_end_offset("n1") == 3
    assert part.replicate("n3") == 2 and part.isr == ["n1", "n3"], "catching up rejoins the ISR"


def test_leader_election_drops_records_above_the_high_watermark() -> None:
    clock = FakeClock(start=100.0)
    part = ReplicatedPartition("orders", 0, ["n1", "n2"], min_insync=1, clock=clock)
    part.produce("ann", "committed", acks=Acks.ALL)
    part.produce("ann", "leader-only-1", acks=Acks.LEADER)
    part.produce("ann", "leader-only-2", acks=Acks.LEADER)
    assert part.high_watermark == 1 and part.log_end_offset("n1") == 3

    change = part.fail("n1")
    assert change is not None
    assert (change.old_leader, change.new_leader, change.lost_records) == ("n1", "n2", 2)
    assert part.leader == "n2" and part.isr == ["n2"]
    assert [r.value for r in part.fetch(0)] == ["committed"]

    assert part.recover("n1") == 2, "the old leader rolls back its unreplicated tail"
    assert part.log_end_offset("n1") == part.log_end_offset("n2") == 1
    assert part.isr == ["n1", "n2"]
    second = part.fail("n2")
    assert second is not None and second.new_leader == "n1" and second.lost_records == 0


def test_concurrent_appends_are_serialised_into_one_gap_free_log() -> None:
    log = SegmentedLog("orders", 0, segment_bytes=1_024, index_interval=256)
    writers, per_writer = 8, 50

    def produce(writer: int) -> list[int]:
        return [log.append(f"w{writer}", f"v{i}", 1_000.0 + i).offset for i in range(per_writer)]

    with ThreadPoolExecutor(max_workers=writers) as pool:
        offsets = sorted(off for batch in pool.map(produce, range(writers)) for off in batch)

    total = writers * per_writer
    assert offsets == list(range(total)), "no duplicate and no skipped offset under contention"
    assert log.end_offset == total
    stored = log.read(0, total + 10)
    assert [r.offset for r in stored] == list(range(total))
    infos = log.segments()
    assert sum(info.records for info in infos) == total
    assert infos[-1].base_offset + infos[-1].records == total


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"replicas": ["n1"]}, ValidationError),
        ({"replicas": ["n1", "n1"]}, ValidationError),
        ({"replicas": ["n1", "n2"], "min_insync": 3}, ValidationError),
        ({"replicas": ["n1", "n2"], "min_insync": 0}, ValidationError),
    ],
)
def test_replica_configuration_is_validated(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        ReplicatedPartition("orders", 0, **kwargs)  # type: ignore[arg-type]


def test_log_configuration_and_unknown_replicas_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SegmentedLog("orders", 0, segment_bytes=0)
    log = filled_log(10)
    with pytest.raises(ValidationError):
        log.read(0, 0)
    with pytest.raises(ValidationError):
        log.truncate(-1)
    part = ReplicatedPartition("orders", 0, ["n1", "n2"], min_insync=1, clock=FakeClock())
    with pytest.raises(NotFoundError):
        part.log_end_offset("nobody")
