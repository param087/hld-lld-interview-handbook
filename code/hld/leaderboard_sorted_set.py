"""A leaderboard in three layers: a skip-list sorted set, sharding, and periodic boards.

The gaming-leaderboard case study compressed into one module:

* ``SortedSet`` is a skip list with span counters plus a member-to-score dict - the same
  pair of structures a Redis sorted set uses. Spans are what make ``rank`` O(log n) instead
  of O(n), and what make "the page starting at rank 5,000" a jump rather than a scan.
* ``ShardedLeaderboard`` partitions members across N sorted sets by a stable hash. Writes
  stay single-shard; every global read becomes a scatter-gather, and ``rank`` becomes a sum
  of "how many members do you hold above this score".
* ``PeriodicLeaderboards`` writes each score into one board per period (all-time, daily,
  weekly) and lets old period keys expire, because a daily board is a different key, not a
  filter over the all-time board.
"""

from __future__ import annotations

import heapq
import itertools
import math
import random
import threading
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from common import Clock, NotFoundError, SystemClock, ValidationError


# --8<-- [start:models]
@dataclass(frozen=True, slots=True)
class Entry:
    """A member and its score, with no opinion about where it ranks."""

    member: str
    score: float


@dataclass(frozen=True, slots=True)
class RankedEntry:
    """One row of a rendered board: a 0-based global rank, a member and a score."""

    rank: int
    member: str
    score: float


def order_key(score: float, member: str) -> tuple[float, str]:
    """Highest score first, ties broken by member ascending.

    A total order is not optional. With ties broken arbitrarily, two players on 1,000 points
    swap places between two reads of the same board and the client shows a flicker; worse,
    a cursor built from the last row of a page can skip or repeat rows.
    """
    return (-score, member)


# --8<-- [end:models]


@dataclass(slots=True)
class _Node:
    member: str
    score: float
    forward: list[_Node | None] = field(default_factory=list)
    span: list[int] = field(default_factory=list)


# --8<-- [start:skiplist]
class SortedSet:
    """A skip list with span counters, plus a dict from member to score.

    Complexity, which is the whole reason this structure and not a sorted list:

    * ``add`` / ``remove`` / ``score_of`` / ``rank`` - O(log n) expected (``score_of`` O(1))
    * ``top(k)`` - O(k): the level-0 list is already in board order
    * ``page(start, count)`` - O(log n + count) by following spans, not O(start + count)

    ``span[i]`` is the number of level-0 steps a level-``i`` pointer skips. Summing the spans
    you traverse while searching gives the rank for free, which is exactly what a SQL
    ``ORDER BY score DESC LIMIT ... OFFSET 5000`` cannot do without scanning 5,000 rows.
    """

    _MAX_LEVEL = 16
    _P = 0.25

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._head = _Node("", 0.0, [None] * self._MAX_LEVEL, [0] * self._MAX_LEVEL)
        self._level = 1
        self._length = 0
        self._scores: dict[str, float] = {}

    def __len__(self) -> int:
        return self._length

    def __contains__(self, member: object) -> bool:
        return member in self._scores

    def score_of(self, member: str) -> float | None:
        """O(1) - this is why a sorted set is two structures and not one."""
        return self._scores.get(member)

    def add(self, member: str, score: float) -> None:
        """Insert a member, or move one that is already present. O(log n) expected."""
        current = self._scores.get(member)
        if current is not None:
            if current == score:
                return
            self._unlink(member, current)
        self._link(member, score)
        self._scores[member] = score

    def remove(self, member: str) -> bool:
        current = self._scores.pop(member, None)
        if current is None:
            return False
        self._unlink(member, current)
        return True

    def count_above(self, score: float, member: str = "") -> int:
        """How many members sort strictly before ``(score, member)``.

        With the default empty member this counts members with a strictly greater score,
        which is what a shard contributes to a global rank it does not own.
        """
        key = order_key(score, member)
        node, traversed = self._head, 0
        for level in reversed(range(self._level)):
            nxt = node.forward[level]
            while nxt is not None and order_key(nxt.score, nxt.member) < key:
                traversed += node.span[level]
                node = nxt
                nxt = node.forward[level]
        return traversed

    def rank(self, member: str) -> int:
        """0-based rank on this set. O(log n)."""
        score = self._scores.get(member)
        if score is None:
            raise NotFoundError(f"{member!r} is not on this board")
        return self.count_above(score, member)

    def top(self, limit: int) -> list[Entry]:
        """The first ``limit`` entries in board order. O(limit)."""
        if limit <= 0:
            raise ValidationError("limit must be positive")
        out: list[Entry] = []
        node = self._head.forward[0]
        while node is not None and len(out) < limit:
            out.append(Entry(node.member, node.score))
            node = node.forward[0]
        return out

    def page(self, start: int, count: int) -> list[Entry]:
        """``count`` entries beginning at 0-based rank ``start``. O(log n + count)."""
        if start < 0 or count <= 0:
            raise ValidationError("start must be non-negative and count positive")
        target = start + 1  # spans count level-0 steps, so ranks are 1-based inside the walk
        node, traversed = self._head, 0
        for level in reversed(range(self._level)):
            nxt = node.forward[level]
            while nxt is not None and traversed + node.span[level] <= target:
                traversed += node.span[level]
                node = nxt
                nxt = node.forward[level]
        if traversed != target:
            return []
        out: list[Entry] = []
        cursor: _Node | None = node
        while cursor is not None and len(out) < count:
            out.append(Entry(cursor.member, cursor.score))
            cursor = cursor.forward[0]
        return out

    # -- internals -------------------------------------------------------------------
    def _random_level(self) -> int:
        level = 1
        while level < self._MAX_LEVEL and self._rng.random() < self._P:
            level += 1
        return level

    def _link(self, member: str, score: float) -> None:
        update: list[_Node] = [self._head] * self._MAX_LEVEL
        rank_at = [0] * self._MAX_LEVEL
        key = order_key(score, member)
        node = self._head
        for level in reversed(range(self._level)):
            rank_at[level] = 0 if level == self._level - 1 else rank_at[level + 1]
            nxt = node.forward[level]
            while nxt is not None and order_key(nxt.score, nxt.member) < key:
                rank_at[level] += node.span[level]
                node = nxt
                nxt = node.forward[level]
            update[level] = node
        level_count = self._random_level()
        if level_count > self._level:
            for level in range(self._level, level_count):
                rank_at[level] = 0
                update[level] = self._head
                self._head.span[level] = self._length
            self._level = level_count
        fresh = _Node(member, score, [None] * level_count, [0] * level_count)
        for level in range(level_count):
            fresh.forward[level] = update[level].forward[level]
            update[level].forward[level] = fresh
            fresh.span[level] = update[level].span[level] - (rank_at[0] - rank_at[level])
            update[level].span[level] = (rank_at[0] - rank_at[level]) + 1
        for level in range(level_count, self._level):
            update[level].span[level] += 1
        self._length += 1

    def _unlink(self, member: str, score: float) -> None:
        update: list[_Node] = [self._head] * self._MAX_LEVEL
        key = order_key(score, member)
        node = self._head
        for level in reversed(range(self._level)):
            nxt = node.forward[level]
            while nxt is not None and order_key(nxt.score, nxt.member) < key:
                node = nxt
                nxt = node.forward[level]
            update[level] = node
        target = node.forward[0]
        if target is None or target.member != member:
            return
        for level in range(self._level):
            if update[level].forward[level] is target:
                update[level].span[level] += target.span[level] - 1
                update[level].forward[level] = target.forward[level]
            else:
                update[level].span[level] -= 1
        while self._level > 1 and self._head.forward[self._level - 1] is None:
            self._level -= 1
        self._length -= 1


# --8<-- [end:skiplist]


# --8<-- [start:sharded]
class ShardedLeaderboard:
    """N sorted sets partitioned by a stable hash of the member id.

    Sharding by member spreads writes evenly and keeps ``submit`` a single-shard O(log n)
    operation. The price is on the read side: every global answer is a scatter-gather.
    ``top(k)`` is correct because the global top K is a subset of the union of the shards'
    local top Ks, so N lists of K merge into the answer. ``rank`` is a sum, because a global
    rank is "how many members does each shard hold above my score".

    One lock per shard, so writes to different shards never contend.
    """

    def __init__(self, shards: int = 4, seed: int | None = None, max_score: float = 1e9) -> None:
        if shards <= 0:
            raise ValidationError("a leaderboard needs at least one shard")
        self._max_score = max_score
        self._sets = [
            SortedSet(random.Random(seed + i) if seed is not None else None) for i in range(shards)
        ]
        self._locks = [threading.Lock() for _ in range(shards)]

    def __len__(self) -> int:
        return sum(len(s) for s in self._sets)

    @property
    def shard_count(self) -> int:
        return len(self._sets)

    def shard_for(self, member: str) -> int:
        """CRC32 rather than ``hash()``: the placement must survive a process restart."""
        return zlib.crc32(member.encode()) % len(self._sets)

    def submit(self, member: str, score: float, *, best_only: bool = True) -> float:
        """Record a score and return the stored value.

        Validation is the unglamorous half of the crux. A score of ``inf`` or 10^12 is not a
        bug you fix later - it is an entry nobody can ever displace, and support deletes it
        by hand. ``best_only`` makes the write idempotent for replays: re-delivering the same
        event cannot lower a player's best.
        """
        if not member:
            raise ValidationError("member id is required")
        if not math.isfinite(score) or score < 0:
            raise ValidationError(f"score must be a finite non-negative number, got {score!r}")
        if score > self._max_score:
            raise ValidationError(f"score {score:g} exceeds the ceiling {self._max_score:g}")
        index = self.shard_for(member)
        with self._locks[index]:
            current = self._sets[index].score_of(member)
            if best_only and current is not None and current >= score:
                return current
            self._sets[index].add(member, score)
            return score

    def score_of(self, member: str) -> float:
        index = self.shard_for(member)
        with self._locks[index]:
            score = self._sets[index].score_of(member)
        if score is None:
            raise NotFoundError(f"{member!r} is not on this board")
        return score

    def top(self, limit: int = 10) -> list[RankedEntry]:
        """Scatter to every shard, merge the local top lists, take the first ``limit``."""
        if limit <= 0:
            raise ValidationError("limit must be positive")
        pages = []
        for index, sset in enumerate(self._sets):
            with self._locks[index]:
                pages.append(sset.top(limit))
        merged = heapq.merge(*pages, key=lambda e: order_key(e.score, e.member))
        return [
            RankedEntry(rank, entry.member, entry.score)
            for rank, entry in enumerate(itertools.islice(merged, limit))
        ]

    def rank(self, member: str) -> int:
        """Global 0-based rank: sum over shards of "members you hold above this key"."""
        score = self.score_of(member)
        total = 0
        for index, sset in enumerate(self._sets):
            with self._locks[index]:
                total += sset.count_above(score, member)
        return total

    def neighbours(self, member: str, radius: int = 2) -> list[RankedEntry]:
        """The relative board: the ``radius`` players either side of this one.

        Every global neighbour of a member sits within ``radius`` positions of that member's
        split point *inside its own shard*, so one window per shard is a superset of the
        answer. Merge the windows, then slice around the member.
        """
        if radius < 0:
            raise ValidationError("radius must be non-negative")
        score = self.score_of(member)
        window: list[Entry] = []
        for index, sset in enumerate(self._sets):
            with self._locks[index]:
                local = sset.count_above(score, member)
                window.extend(sset.page(max(0, local - radius), 2 * radius + 1))
        window.sort(key=lambda e: order_key(e.score, e.member))
        centre = next(i for i, entry in enumerate(window) if entry.member == member)
        low = max(0, centre - radius)
        rank = self.rank(member)
        return [
            RankedEntry(rank - (centre - low) + offset, entry.member, entry.score)
            for offset, entry in enumerate(window[low : centre + radius + 1])
        ]


# --8<-- [end:sharded]


# --8<-- [start:periodic]
@dataclass(frozen=True, slots=True)
class Period:
    """One board family: how wide a bucket is, and how long a closed bucket survives."""

    name: str
    bucket_s: int  # 0 means the board never rotates (all-time)
    ttl_s: int = 0

    def key(self, now: float) -> str:
        if self.bucket_s == 0:
            return self.name
        return f"{self.name}:{int(now // self.bucket_s)}"

    def expires_at(self, now: float) -> float | None:
        """End of the bucket plus the grace period, computed from the bucket, not the write.

        Deriving the expiry from the bucket index keeps it idempotent: a late submission
        cannot extend yesterday's board by another day.
        """
        if self.bucket_s == 0:
            return None
        return float((int(now // self.bucket_s) + 1) * self.bucket_s + self.ttl_s)


class PeriodicLeaderboards:
    """One board per period, keyed ``<name>:<bucket>``, retired by TTL rather than by a job.

    A daily board is not a filter over the all-time board - there is no index that could
    answer "scores submitted since midnight" from a sorted set keyed by score. So a submit
    writes every period's current key, which is why the write amplification is exactly the
    number of periods you offer.
    """

    def __init__(
        self,
        periods: Sequence[Period],
        clock: Clock | None = None,
        shards: int = 4,
        seed: int | None = None,
    ) -> None:
        if not periods:
            raise ValidationError("at least one period is required")
        self._periods = tuple(periods)
        self._clock = clock or SystemClock()
        self._shards = shards
        self._seed = seed
        self._boards: dict[str, ShardedLeaderboard] = {}
        self._expiry: dict[str, float] = {}
        self._lock = threading.Lock()

    def submit(self, member: str, score: float) -> list[str]:
        """Write the score into every period's current board; return the keys touched."""
        now = self._clock.now()
        keys: list[str] = []
        for period in self._periods:
            key = period.key(now)
            self._board(key, period, now).submit(member, score)
            keys.append(key)
        return keys

    def board(self, name: str) -> ShardedLeaderboard:
        key = next(p.key(self._clock.now()) for p in self._periods if p.name == name)
        with self._lock:
            if key not in self._boards:
                raise NotFoundError(f"board {key!r} has no scores yet")
            return self._boards[key]

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._boards)

    def expire(self) -> list[str]:
        """Drop every board whose bucket closed more than its grace period ago."""
        now = self._clock.now()
        with self._lock:
            dead = sorted(key for key, at in self._expiry.items() if at <= now)
            for key in dead:
                del self._boards[key]
                del self._expiry[key]
        return dead

    def _board(self, key: str, period: Period, now: float) -> ShardedLeaderboard:
        with self._lock:
            board = self._boards.get(key)
            if board is None:
                board = ShardedLeaderboard(self._shards, seed=self._seed)
                self._boards[key] = board
                expiry = period.expires_at(now)
                if expiry is not None:
                    self._expiry[key] = expiry
            return board


# --8<-- [end:periodic]


DEMO_PLAYERS = {
    "ana": 9800.0,
    "bo": 7400.0,
    "cy": 9800.0,
    "dee": 6100.0,
    "eli": 8850.0,
    "fin": 7400.0,
    "gus": 5200.0,
    "hal": 9990.0,
    "ivy": 8100.0,
    "jo": 4300.0,
}
DAY = 86_400


def main() -> None:
    from common import FakeClock

    single = SortedSet(random.Random(42))
    for member, score in DEMO_PLAYERS.items():
        single.add(member, score)
    top = ", ".join(f"{e.member} {e.score:.0f}" for e in single.top(3))
    print(f"one sorted set, {len(single)} members -> top 3: {top}")
    print(f"rank('cy')={single.rank('cy')} rank('ana')={single.rank('ana')}  (tie on 9800, id breaks it)")
    print(f"page(start=4, count=3) -> {[e.member for e in single.page(4, 3)]}")

    board = ShardedLeaderboard(shards=4, seed=7)
    for member, score in DEMO_PLAYERS.items():
        board.submit(member, score)
    spread = [sum(1 for m in DEMO_PLAYERS if board.shard_for(m) == s) for s in range(board.shard_count)]
    print(f"sharded over {board.shard_count} shards, members per shard: {spread}")
    print("scatter-gather top 5:")
    for row in board.top(5):
        print(f"  #{row.rank + 1:<2} {row.member:<4} {row.score:.0f}")
    print(f"global rank('ivy') = {board.rank('ivy')} (sum of per-shard count_above)")
    around = ", ".join(f"#{r.rank + 1} {r.member}" for r in board.neighbours("ivy", radius=2))
    print(f"relative board around 'ivy': {around}")

    print(f"submit('jo', 100) with best_only -> stored {board.submit('jo', 100.0):.0f}")
    try:
        board.submit("jo", float("inf"))
    except ValidationError as exc:
        print(f"submit('jo', inf) -> ValidationError: {exc}")

    clock = FakeClock(start=1_700_000_000)
    periodic = PeriodicLeaderboards(
        [Period("alltime", 0), Period("daily", DAY, ttl_s=DAY)], clock=clock, shards=2, seed=7
    )
    print(f"submit writes keys {periodic.submit('ana', 500.0)}")
    clock.advance(2 * DAY)
    print(f"submit writes keys {periodic.submit('ana', 700.0)}")
    print(f"keys held: {periodic.keys()}")
    print(f"expire() drops {periodic.expire()}, leaving {periodic.keys()}")


if __name__ == "__main__":
    main()
