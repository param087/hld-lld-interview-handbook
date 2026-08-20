from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.fanout import FeedService, decode_cursor, encode_cursor


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000.0)


def test_normal_user_posts_are_pushed_to_followers(clock: FakeClock) -> None:
    feed = FeedService(celebrity_threshold=100, clock=clock)
    feed.follow("ann", "dan")
    post = feed.post("dan", "hi")
    assert [p.id for p in feed.get_feed("ann").posts] == [post.id]
    assert feed.get_feed("bob").posts == []  # bob does not follow dan


def test_celebrity_posts_are_pulled_not_pushed(clock: FakeClock) -> None:
    feed = FeedService(celebrity_threshold=1, clock=clock)
    feed.follow("ann", "star")
    feed.follow("bob", "star")  # 2 followers > threshold 1
    assert feed.is_celebrity("star")
    post = feed.post("star", "big news")
    assert len(feed._feed_cache["ann"]) == 0  # nothing was fanned out
    assert [p.id for p in feed.get_feed("ann").posts] == [post.id]  # but it is merged at read


def test_feed_merges_both_sources_newest_first(clock: FakeClock) -> None:
    feed = FeedService(celebrity_threshold=1, clock=clock)
    feed.follow("ann", "star")
    feed.follow("bob", "star")
    feed.follow("ann", "dan")
    contents = []
    for author, text in [("dan", "d1"), ("star", "s1"), ("dan", "d2"), ("star", "s2")]:
        clock.advance(1)
        feed.post(author, text)
        contents.append(text)
    assert [p.content for p in feed.get_feed("ann").posts] == list(reversed(contents))


def test_cursor_pagination_is_stable_and_complete(clock: FakeClock) -> None:
    feed = FeedService(clock=clock)
    feed.follow("ann", "dan")
    for i in range(7):
        clock.advance(1)
        feed.post("dan", f"p{i}")
    seen, cursor = [], None
    for _ in range(10):
        page = feed.get_feed("ann", limit=3, cursor=cursor)
        seen += [p.content for p in page.posts]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == [f"p{i}" for i in range(6, -1, -1)]
    assert decode_cursor(encode_cursor(12.5, "post-9")) == (12.5, "post-9")
    with pytest.raises(ValidationError):
        decode_cursor("not-a-cursor")


def test_deleted_posts_disappear_without_touching_caches(clock: FakeClock) -> None:
    feed = FeedService(clock=clock)
    feed.follow("ann", "dan")
    keep = feed.post("dan", "keep")
    gone = feed.post("dan", "gone")
    feed.delete_post(gone.id)
    assert [p.id for p in feed.get_feed("ann").posts] == [keep.id]
    assert len(feed._feed_cache["ann"]) == 2  # tombstone, not a scrub


def test_validation_errors(clock: FakeClock) -> None:
    feed = FeedService(clock=clock)
    with pytest.raises(ValidationError):
        feed.follow("ann", "ann")
    with pytest.raises(ValidationError):
        feed.post("ann", "   ")
    with pytest.raises(ValidationError):
        feed.get_feed("ann", limit=0)


def test_concurrent_posts_all_land_in_follower_feeds(clock: FakeClock) -> None:
    feed = FeedService(feed_cache_size=10_000, clock=clock)
    for fan in ("ann", "bob"):
        feed.follow(fan, "dan")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: feed.post("dan", f"p{i}"), range(500)))
    assert len(feed.get_feed("ann", limit=1000).posts) == 500
    assert len(feed.get_feed("bob", limit=1000).posts) == 500
