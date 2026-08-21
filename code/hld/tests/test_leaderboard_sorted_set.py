"""Tests for the leaderboard: skip-list ranks, scatter-gather sharding and periodic boards."""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, NotFoundError, ValidationError
from hld.leaderboard_sorted_set import (
    DAY,
    Period,
    PeriodicLeaderboards,
    ShardedLeaderboard,
    SortedSet,
    order_key,
)

PLAYERS = {
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


def _expected(model: dict[str, float]) -> list[str]:
    return [m for m, _ in sorted(model.items(), key=lambda kv: order_key(kv[1], kv[0]))]


def _filled(rng_seed: int = 42) -> SortedSet:
    sset = SortedSet(random.Random(rng_seed))
    for member, score in PLAYERS.items():
        sset.add(member, score)
    return sset


def test_ties_are_broken_by_member_so_the_order_is_total() -> None:
    sset = _filled()
    assert [e.member for e in sset.top(4)] == ["hal", "ana", "cy", "eli"]
    assert sset.rank("ana") == 1 and sset.rank("cy") == 2  # both on 9800
    assert sset.score_of("cy") == 9800.0
    assert sset.score_of("nobody") is None
    with pytest.raises(NotFoundError):
        sset.rank("nobody")


@pytest.mark.parametrize(("start", "count"), [(0, 3), (4, 3), (7, 5), (9, 1), (10, 2)])
def test_page_matches_the_full_ordering(start: int, count: int) -> None:
    sset = _filled()
    expected = _expected(PLAYERS)[start : start + count]
    assert [e.member for e in sset.page(start, count)] == expected


def test_updating_a_score_moves_the_member_without_duplicating_it() -> None:
    sset = _filled()
    assert len(sset) == 10
    sset.add("jo", 9999.0)  # last place to first
    assert len(sset) == 10
    assert sset.rank("jo") == 0
    assert sset.top(1)[0].member == "jo"
    assert sset.remove("jo") is True
    assert sset.remove("jo") is False
    assert len(sset) == 9
    assert sset.rank("hal") == 0


def test_count_above_ignores_the_member_when_none_is_given() -> None:
    sset = _filled()
    assert sset.count_above(9800.0) == 1  # only hal scores strictly higher
    assert sset.count_above(9800.0, "ana") == 1
    assert sset.count_above(9800.0, "cy") == 2  # ana ties and sorts first
    assert sset.count_above(0.0) == 10


def test_skip_list_agrees_with_a_naive_model_under_random_operations() -> None:
    rng = random.Random(42)
    sset = SortedSet(random.Random(1234))
    model: dict[str, float] = {}
    for step in range(400):
        member = f"p{rng.randrange(60)}"
        if model and rng.random() < 0.25:
            victim = rng.choice(sorted(model))
            model.pop(victim)
            sset.remove(victim)
        else:
            score = float(rng.randrange(0, 500))
            model[member] = score
            sset.add(member, score)
        if step % 37 == 0 and model:
            order = _expected(model)
            assert len(sset) == len(model)
            assert [e.member for e in sset.top(5)] == order[:5]
            probe = rng.choice(order)
            assert sset.rank(probe) == order.index(probe)
            offset = rng.randrange(len(order))
            assert [e.member for e in sset.page(offset, 4)] == order[offset : offset + 4]


@pytest.mark.parametrize("shards", [1, 2, 4, 8])
def test_sharded_top_and_rank_match_a_single_sorted_set(shards: int) -> None:
    board = ShardedLeaderboard(shards=shards, seed=7)
    for member, score in PLAYERS.items():
        board.submit(member, score)
    order = _expected(PLAYERS)
    assert len(board) == len(PLAYERS)
    assert [row.member for row in board.top(5)] == order[:5]
    assert [row.rank for row in board.top(5)] == [0, 1, 2, 3, 4]
    for member in PLAYERS:
        assert board.rank(member) == order.index(member)


def test_neighbours_returns_the_global_window_around_a_player() -> None:
    board = ShardedLeaderboard(shards=4, seed=7)
    for member, score in PLAYERS.items():
        board.submit(member, score)
    order = _expected(PLAYERS)
    rows = board.neighbours("ivy", radius=2)
    centre = order.index("ivy")
    assert [r.member for r in rows] == order[centre - 2 : centre + 3]
    assert [r.rank for r in rows] == list(range(centre - 2, centre + 3))
    # At the very top the window is clipped, never wrapped or negative.
    leader = board.neighbours(order[0], radius=3)
    assert [r.member for r in leader] == order[:4]
    assert leader[0].rank == 0


@pytest.mark.parametrize("bad", [-1.0, float("inf"), float("nan"), 2e9])
def test_score_validation_rejects_impossible_values(bad: float) -> None:
    board = ShardedLeaderboard(shards=2, seed=7, max_score=1e9)
    with pytest.raises(ValidationError):
        board.submit("ana", bad)
    with pytest.raises(ValidationError):
        board.submit("", 10.0)
    with pytest.raises(NotFoundError):
        board.score_of("ana")


def test_best_only_makes_a_replayed_submission_harmless() -> None:
    board = ShardedLeaderboard(shards=2, seed=7)
    assert board.submit("ana", 500.0) == 500.0
    assert board.submit("ana", 900.0) == 900.0
    assert board.submit("ana", 500.0) == 900.0  # replay cannot lower a best score
    assert board.submit("ana", 100.0, best_only=False) == 100.0  # explicit overwrite still works
    assert board.score_of("ana") == 100.0


def test_periodic_boards_use_one_key_per_bucket_and_expire_by_bucket() -> None:
    clock = FakeClock(start=1_700_000_000)
    boards = PeriodicLeaderboards(
        [Period("alltime", 0), Period("daily", DAY, ttl_s=DAY)], clock=clock, shards=2, seed=7
    )
    assert boards.submit("ana", 500.0) == ["alltime", "daily:19675"]
    clock.advance(2 * DAY)
    assert boards.submit("ana", 700.0) == ["alltime", "daily:19677"]
    assert boards.keys() == ["alltime", "daily:19675", "daily:19677"]

    assert boards.expire() == ["daily:19675"]  # closed 2 days ago, grace period spent
    assert boards.keys() == ["alltime", "daily:19677"]
    assert boards.board("alltime").score_of("ana") == 700.0
    assert boards.board("daily").score_of("ana") == 700.0
    with pytest.raises(NotFoundError):
        boards.board("daily").score_of("bo")


def test_concurrent_submits_across_shards_keep_the_board_consistent() -> None:
    board = ShardedLeaderboard(shards=8, seed=7)
    writers, per_writer = 8, 150

    def play(worker: int) -> None:
        for i in range(per_writer):
            board.submit(f"p{worker}-{i}", float(worker * 1000 + i))

    with ThreadPoolExecutor(max_workers=writers) as pool:
        list(pool.map(play, range(writers)))

    assert len(board) == writers * per_writer
    top = board.top(3)
    assert [row.member for row in top] == ["p7-149", "p7-148", "p7-147"]
    assert board.rank("p7-149") == 0
    assert board.rank("p0-0") == writers * per_writer - 1
