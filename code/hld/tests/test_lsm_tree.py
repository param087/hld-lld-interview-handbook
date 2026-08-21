from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.lsm_tree import BloomFilter, Entry, LsmTree, SSTable, WriteAheadLog


def test_put_get_overwrite_and_delete_across_flushes() -> None:
    tree = LsmTree(memtable_limit=3, block_size=2)
    for i in range(9):
        tree.put(f"k{i}", f"v{i}")
    assert len(tree.sstables) == 3 and tree.memtable_size == 0
    tree.put("k1", "new")
    tree.delete("k2")
    assert tree.get("k1") == "new"  # memtable shadows the table version
    assert tree.get("k2") is None  # tombstone in the memtable
    assert tree.get("k8") == "v8"  # still only in a table
    tree.flush()
    assert tree.get("k1") == "new" and tree.get("k2") is None
    assert tree.lookup("k2").source == "sstable 4"  # the tombstone is found, not the old value
    assert tree.get("missing") is None


def test_bloom_filter_has_no_false_negatives_and_few_false_positives() -> None:
    bloom = BloomFilter(expected_keys=1_000, bits_per_key=10)
    present = [f"user:{i}" for i in range(1_000)]
    for key in present:
        bloom.add(key)
    assert all(bloom.might_contain(key) for key in present)
    absent = [f"ghost:{i}" for i in range(10_000)]
    false_positives = sum(1 for key in absent if bloom.might_contain(key))
    assert false_positives / len(absent) < 0.03  # ~1% expected at 10 bits per key
    with pytest.raises(ValidationError):
        BloomFilter(expected_keys=0)


def test_flush_writes_a_sorted_table_and_truncates_the_wal() -> None:
    wal = WriteAheadLog()
    tree = LsmTree(memtable_limit=100, wal=wal)
    for key in ["m", "c", "x", "a"]:
        tree.put(key, "v")
    assert len(wal) == 4 and tree.flush() is not None
    (table,) = tree.sstables
    assert [entry.key for entry in table] == ["a", "c", "m", "x"]
    assert table.key_range == ("a", "x")
    assert len(wal) == 0 and tree.memtable_size == 0
    assert tree.flush() is None  # nothing to flush


@pytest.mark.parametrize("block_size", [1, 2, 3, 7, 50])
def test_sstable_sparse_index_finds_every_key_and_misses_absent_ones(block_size: int) -> None:
    entries = [Entry(f"k{i:03d}", str(i)) for i in range(0, 40, 2)]
    table = SSTable(entries, table_id=1, block_size=block_size)
    for entry in entries:
        assert table.get(entry.key) == entry
    assert table.get("k001") is None  # between two keys
    assert table.get("a") is None  # before the first key
    assert table.get("z") is None  # after the last key
    assert len(table) == 20 and table.size == sum(entry.size for entry in entries)


def test_recover_replays_the_wal_into_the_memtable() -> None:
    wal = WriteAheadLog()
    tree = LsmTree(memtable_limit=2, wal=wal)
    tree.put("a", "1")
    tree.put("b", "2")  # flushed
    tree.put("c", "3")  # only in WAL + memtable
    tree.delete("a")  # also only in WAL + memtable -> flushes at limit 2
    tree.put("d", "4")
    assert len(wal) == 1
    recovered = LsmTree.recover(wal, tree.sstables, memtable_limit=2)
    assert recovered.get("d") == "4"
    assert recovered.get("c") == "3"
    assert recovered.get("a") is None
    assert recovered.get("b") == "2"
    assert recovered.memtable_size == 1 and len(recovered.sstables) == 2
    recovered.put("e", "5")  # the next table id continues after the recovered ones
    assert [table.table_id for table in recovered.sstables] == [1, 2, 3]


def test_compaction_keeps_newest_version_and_drops_tombstones() -> None:
    tree = LsmTree(memtable_limit=2)
    tree.put("a", "old")
    tree.put("b", "1")
    tree.put("a", "new")
    tree.delete("b")
    tree.put("c", "3")
    tree.put("d", "4")
    before = tree.stats()
    assert before.tables == 3 and before.table_entries == 6 and before.live_entries == 3
    assert before.space_amplification > 1.0
    merged = tree.compact()
    assert merged is not None and [entry.key for entry in merged] == ["a", "c", "d"]
    after = tree.stats()
    assert after.tables == 1 and after.table_entries == 3
    assert after.space_amplification == pytest.approx(1.0)
    assert tree.get("a") == "new" and tree.get("b") is None
    assert tree.lookup("b").block_reads == 0  # the Bloom filter answers the miss
    assert tree.compact() is None  # a single table has nothing to merge with


def test_scan_merges_memtable_and_tables_in_key_order() -> None:
    tree = LsmTree(memtable_limit=3)
    for key, value in [("e", "1"), ("a", "1"), ("c", "1"), ("b", "1"), ("d", "1"), ("a", "2")]:
        tree.put(key, value)
    tree.delete("c")  # stays in the memtable
    assert tree.scan("a", "e") == [("a", "2"), ("b", "1"), ("d", "1")]
    assert tree.scan("e", "z") == [("e", "1")]
    assert tree.scan("x", "z") == []


def test_write_amplification_is_wal_plus_flushes_plus_compaction() -> None:
    wal = WriteAheadLog()
    tree = LsmTree(memtable_limit=2, wal=wal)
    for key in ["a", "b", "a", "c"]:
        tree.put(key, "vv")  # 3 bytes each: 12 user bytes, two flushes of 6
    tree.compact()
    stats = tree.stats()
    assert stats.user_bytes == 12
    assert wal.bytes_appended == 12
    assert stats.disk_bytes_written == 12 + 12 + 9  # WAL + two flushes + merged table (a, b, c)
    assert stats.write_amplification == pytest.approx(33 / 12)


def test_validation_errors() -> None:
    with pytest.raises(ValidationError):
        LsmTree(memtable_limit=0)
    with pytest.raises(ValidationError):
        LsmTree().put("", "v")
    with pytest.raises(ValidationError):
        SSTable([], table_id=1)
    with pytest.raises(ValidationError):
        SSTable([Entry("b", "1"), Entry("a", "2")], table_id=1)  # unsorted
    with pytest.raises(ValidationError):
        SSTable([Entry("a", "1"), Entry("a", "2")], table_id=1)  # duplicate key
    with pytest.raises(ValidationError):
        SSTable([Entry("a", "1")], table_id=1, block_size=0)


def test_concurrent_writes_and_compactions_lose_nothing() -> None:
    tree = LsmTree(memtable_limit=5, block_size=3)

    def writer(worker: int) -> None:
        for i in range(40):
            tree.put(f"w{worker}:k{i:02d}", f"{worker}-{i}")
            if i % 10 == 9:
                tree.compact()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(writer, range(8)))
    tree.flush()
    tree.compact()
    for worker in range(8):
        for i in range(40):
            assert tree.get(f"w{worker}:k{i:02d}") == f"{worker}-{i}"
    stats = tree.stats()
    assert stats.tables == 1 and stats.live_entries == 320 and stats.table_entries == 320
