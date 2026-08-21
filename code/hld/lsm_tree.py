"""A toy log-structured merge tree: WAL, memtable, SSTable flush, Bloom filters and compaction.

What the module demonstrates, in the order an interviewer asks about it:

* ``WriteAheadLog``: every write is appended to the log before it touches the memtable, so a
  crash loses nothing that was acknowledged; ``LsmTree.recover`` replays it.
* ``MemTable``: writes land in memory and are flushed, sorted, into an immutable ``SSTable``
  once ``memtable_limit`` entries have accumulated. Deletes are tombstones, not removals.
* ``SSTable``: a sorted run with a Bloom filter and a sparse index, so a point read skips most
  tables without touching them and scans at most one block in the rest.
* ``LsmTree.compact``: a k-way merge of every table into one (a major compaction) that keeps
  the newest version of each key and drops tombstones; ``stats`` turns read, write and space
  amplification into numbers.
"""

from __future__ import annotations

import bisect
import hashlib
import heapq
import math
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from common import ValidationError


# --8<-- [start:bloom]
class BloomFilter:
    """A fixed-size Bloom filter: ``k`` hash positions over ``m`` bits, no false negatives.

    Ten bits per key and k = 7 give about a 1% false-positive rate, which is what RocksDB and
    Cassandra default to. The k positions come from double hashing one SHA-256 digest, so
    every probe costs one hash, not k.
    """

    def __init__(self, expected_keys: int, bits_per_key: int = 10) -> None:
        if expected_keys <= 0 or bits_per_key <= 0:
            raise ValidationError("expected_keys and bits_per_key must be positive")
        self._bits_count = expected_keys * bits_per_key
        self._hashes = max(1, round(bits_per_key * math.log(2)))
        self._bits = 0  # a Python int used as a bitset

    def _positions(self, key: str) -> Iterator[int]:
        digest = hashlib.sha256(key.encode()).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:16], "big") | 1
        for i in range(self._hashes):
            yield (h1 + i * h2) % self._bits_count

    def add(self, key: str) -> None:
        for position in self._positions(key):
            self._bits |= 1 << position

    def might_contain(self, key: str) -> bool:
        """False means "definitely absent"; True means "probably present, go and look"."""
        return all((self._bits >> position) & 1 for position in self._positions(key))


# --8<-- [end:bloom]


# --8<-- [start:wal_memtable]
@dataclass(frozen=True, slots=True)
class Entry:
    """One key-value pair; ``value is None`` is a tombstone (a delete marker)."""

    key: str
    value: str | None

    @property
    def is_tombstone(self) -> bool:
        return self.value is None

    @property
    def size(self) -> int:
        return len(self.key) + len(self.value or "")


class WriteAheadLog:
    """Append-only log of every write, in arrival order.

    A real WAL is a file that is fsync-ed before the client gets its acknowledgement; here it
    is a list so the demo stays deterministic. The contract is the same: nothing reaches the
    memtable that is not in the log first, and once a flush has made the memtable durable as
    an SSTable, the log can be truncated.
    """

    def __init__(self) -> None:
        self._entries: list[Entry] = []
        self._bytes_appended = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def bytes_appended(self) -> int:
        return self._bytes_appended

    def append(self, entry: Entry) -> None:
        self._entries.append(entry)
        self._bytes_appended += entry.size

    def replay(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def truncate(self) -> int:
        """Drop the log after a flush; returns how many entries the flush covered."""
        count = len(self._entries)
        self._entries.clear()
        return count


class MemTable:
    """The in-memory write buffer. A dict here; RocksDB uses a skip list so the flush can
    iterate in key order without sorting first."""

    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def put(self, entry: Entry) -> None:
        self._entries[entry.key] = entry

    def get(self, key: str) -> Entry | None:
        return self._entries.get(key)

    def sorted_entries(self) -> list[Entry]:
        return [self._entries[key] for key in sorted(self._entries)]

    def clear(self) -> None:
        self._entries.clear()


# --8<-- [end:wal_memtable]


# --8<-- [start:sstable]
class SSTable:
    """An immutable, sorted run of entries with a Bloom filter and a sparse index.

    On disk a table is a sequence of 4-64 KB blocks plus a sparse index (the first key of
    every block) and a Bloom filter, both small enough to live in memory. Here ``block_size``
    entries make a block, and ``get`` does what the real thing does: bisect the sparse index
    to one block and scan it, so a probe costs at most one block read.
    """

    def __init__(self, entries: Sequence[Entry], table_id: int, block_size: int = 4) -> None:
        if not entries:
            raise ValidationError("an SSTable needs at least one entry")
        if block_size <= 0:
            raise ValidationError("block_size must be positive")
        keys = [entry.key for entry in entries]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            raise ValidationError("SSTable entries must be sorted by unique key")
        self.table_id = table_id
        self._entries = tuple(entries)
        self._block_size = block_size
        self._block_first_keys = keys[::block_size]
        self._bloom = BloomFilter(len(entries))
        for key in keys:
            self._bloom.add(key)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Entry]:
        return iter(self._entries)

    @property
    def size(self) -> int:
        return sum(entry.size for entry in self._entries)

    @property
    def key_range(self) -> tuple[str, str]:
        return self._entries[0].key, self._entries[-1].key

    def might_contain(self, key: str) -> bool:
        return self._bloom.might_contain(key)

    def get(self, key: str) -> Entry | None:
        """One block read: bisect the sparse index, then scan that block."""
        block = bisect.bisect_right(self._block_first_keys, key) - 1
        if block < 0:
            return None
        start = block * self._block_size
        for entry in self._entries[start : start + self._block_size]:
            if entry.key == key:
                return entry
        return None


# --8<-- [end:sstable]


# --8<-- [start:tree]
@dataclass(frozen=True, slots=True)
class ReadTrace:
    """What one point read cost: where it stopped, blocks read and tables the filters skipped."""

    value: str | None
    source: str
    block_reads: int
    bloom_skips: int


@dataclass(frozen=True, slots=True)
class LsmStats:
    """The three amplification factors every storage-engine argument comes down to."""

    tables: int
    table_entries: int
    live_entries: int
    user_bytes: int
    disk_bytes_written: int
    table_bytes: int
    live_bytes: int

    @property
    def write_amplification(self) -> float:
        """Bytes written to disk (WAL + flushes + compactions) per byte the application wrote."""
        return self.disk_bytes_written / self.user_bytes if self.user_bytes else 0.0

    @property
    def space_amplification(self) -> float:
        """Bytes held by tables per byte of live data (overwritten versions and tombstones)."""
        return self.table_bytes / self.live_bytes if self.live_bytes else 0.0


class LsmTree:
    """Put, get, delete, flush and compact: the parts of RocksDB that fit on one screen.

    ``_lock`` guards the WAL, the memtable, the table list and the counters; every public
    method takes it, so a flush or compaction can never interleave with a put.
    """

    def __init__(
        self, memtable_limit: int = 4, block_size: int = 4, wal: WriteAheadLog | None = None
    ) -> None:
        if memtable_limit <= 0:
            raise ValidationError("memtable_limit must be positive")
        self._memtable_limit = memtable_limit
        self._block_size = block_size
        self._wal = wal if wal is not None else WriteAheadLog()
        self._memtable = MemTable()
        self._tables: list[SSTable] = []  # oldest first; flushes append
        self._next_table_id = 1
        self._user_bytes = 0
        self._table_bytes_written = 0
        self._lock = threading.Lock()

    @classmethod
    def recover(
        cls, wal: WriteAheadLog, tables: Iterable[SSTable], memtable_limit: int = 4, block_size: int = 4
    ) -> LsmTree:
        """Rebuild after a crash: the tables are on disk, the memtable is replayed from the WAL."""
        tree = cls(memtable_limit=memtable_limit, block_size=block_size, wal=wal)
        tree._tables = sorted(tables, key=lambda table: table.table_id)
        tree._next_table_id = max((table.table_id for table in tree._tables), default=0) + 1
        for entry in wal.replay():
            tree._memtable.put(entry)
        return tree

    @property
    def sstables(self) -> list[SSTable]:
        with self._lock:
            return list(self._tables)

    @property
    def memtable_size(self) -> int:
        with self._lock:
            return len(self._memtable)

    def put(self, key: str, value: str) -> None:
        self._write(Entry(key, value))

    def delete(self, key: str) -> None:
        """A delete is a write: the tombstone shadows older versions until compaction drops it."""
        self._write(Entry(key, None))

    def _write(self, entry: Entry) -> None:
        if not entry.key:
            raise ValidationError("key must be non-empty")
        with self._lock:
            self._wal.append(entry)  # durable first ...
            self._memtable.put(entry)  # ... then visible
            self._user_bytes += entry.size
            if len(self._memtable) >= self._memtable_limit:
                self._flush_locked()

    def get(self, key: str) -> str | None:
        return self.lookup(key).value

    def lookup(self, key: str) -> ReadTrace:
        """Memtable first, then tables newest to oldest; the first hit (even a tombstone) wins."""
        with self._lock:
            entry = self._memtable.get(key)
            if entry is not None:
                return ReadTrace(entry.value, "memtable", 0, 0)
            block_reads = bloom_skips = 0
            for table in reversed(self._tables):
                if not table.might_contain(key):
                    bloom_skips += 1
                    continue
                block_reads += 1
                entry = table.get(key)
                if entry is not None:
                    return ReadTrace(entry.value, f"sstable {table.table_id}", block_reads, bloom_skips)
            return ReadTrace(None, "absent", block_reads, bloom_skips)

    def flush(self) -> SSTable | None:
        with self._lock:
            return self._flush_locked()

    def _flush_locked(self) -> SSTable | None:
        if not len(self._memtable):
            return None
        table = self._new_table(self._memtable.sorted_entries())
        self._tables.append(table)
        self._memtable.clear()
        self._wal.truncate()
        return table

    def _new_table(self, entries: Sequence[Entry]) -> SSTable:
        table = SSTable(entries, table_id=self._next_table_id, block_size=self._block_size)
        self._next_table_id += 1
        self._table_bytes_written += table.size
        return table

    def compact(self) -> SSTable | None:
        """Major compaction: merge every table into one, newest version wins, tombstones go.

        ``heapq.merge`` is stable, so feeding it the tables newest-first makes the first entry
        seen for each key the newest one. Dropping tombstones is safe only because no older
        table survives the merge; a leveled engine keeps them until the bottom level.
        """
        with self._lock:
            if len(self._tables) < 2:
                return None
            survivors: list[Entry] = []
            last_key: str | None = None
            for entry in heapq.merge(*reversed(self._tables), key=lambda entry: entry.key):
                if entry.key == last_key:
                    continue  # an older version of a key already emitted
                last_key = entry.key
                if not entry.is_tombstone:
                    survivors.append(entry)
            self._tables = [self._new_table(survivors)] if survivors else []
            return self._tables[0] if self._tables else None

    def scan(self, lo: str, hi: str) -> list[tuple[str, str]]:
        """Live entries with ``lo <= key < hi`` in key order: sorted runs make ranges cheap."""
        with self._lock:
            newest: dict[str, Entry] = {}
            sources: list[Iterable[Entry]] = [self._memtable.sorted_entries(), *reversed(self._tables)]
            for source in sources:
                for entry in source:
                    if lo <= entry.key < hi and entry.key not in newest:
                        newest[entry.key] = entry
            return sorted((key, entry.value) for key, entry in newest.items() if entry.value is not None)

    def stats(self) -> LsmStats:
        with self._lock:
            newest: dict[str, Entry] = {}
            for table in reversed(self._tables):
                for entry in table:
                    newest.setdefault(entry.key, entry)
            live = [entry for entry in newest.values() if not entry.is_tombstone]
            return LsmStats(
                tables=len(self._tables),
                table_entries=sum(len(table) for table in self._tables),
                live_entries=len(live),
                user_bytes=self._user_bytes,
                disk_bytes_written=self._wal.bytes_appended + self._table_bytes_written,
                table_bytes=sum(table.size for table in self._tables),
                live_bytes=sum(entry.size for entry in live),
            )


# --8<-- [end:tree]


def main() -> None:
    wal = WriteAheadLog()
    tree = LsmTree(memtable_limit=4, block_size=2, wal=wal)
    for i in range(1, 9):
        tree.put(f"user:{i}", "v1")
    tree.put("user:2", "v2")
    tree.put("user:5", "v2")
    tree.delete("user:3")
    tree.put("user:9", "v1")
    tables = tree.sstables
    print(
        f"12 writes with memtable_limit=4 -> {len(tables)} flushes, "
        f"tables " + ", ".join(f"#{t.table_id} {t.key_range[0]}..{t.key_range[1]}" for t in tables)
    )

    def show(key: str) -> None:
        trace = tree.lookup(key)
        print(
            f"get {key:<8} -> {str(trace.value):<5} from {trace.source:<9} "
            f"({trace.block_reads} block read(s), {trace.bloom_skips} table(s) skipped by Bloom)"
        )

    for key in ("user:2", "user:7", "user:3", "user:42"):
        show(key)

    tree.put("user:10", "v1")
    print(f"put user:10, not flushed: memtable={tree.memtable_size}, wal={len(wal)} entry, tables={len(tree.sstables)}")
    recovered = LsmTree.recover(wal, tree.sstables, memtable_limit=4, block_size=2)
    print(f"crash and recover: WAL replayed, get user:10 -> {recovered.get('user:10')}, get user:3 -> {recovered.get('user:3')}")

    before = tree.stats()
    tree.compact()
    after = tree.stats()
    print(
        f"before compaction: {before.tables} tables, {before.table_entries} entries for "
        f"{before.live_entries} live keys, space amplification {before.space_amplification:.2f}x"
    )
    print(
        f"after compaction:  {after.tables} table, {after.table_entries} entries for "
        f"{after.live_entries} live keys, space amplification {after.space_amplification:.2f}x"
    )
    print(
        f"write amplification: {after.disk_bytes_written} B on disk (WAL + flushes + compaction) "
        f"/ {after.user_bytes} B written by the app = {after.write_amplification:.1f}x"
    )
    show("user:7")
    print(f"scan user:4..user:7 -> {tree.scan('user:4', 'user:7')}")


if __name__ == "__main__":
    main()
