"""Hybrid fan-out for a news feed: push to followers of normal users, pull from celebrities.

The crux of the news-feed design in one module:

* ``post()`` for a user with <= ``celebrity_threshold`` followers pushes the post id into
  every follower's feed cache (fan-out on write).
* ``post()`` for a celebrity only appends to the author's own timeline; readers pull and
  merge it at read time (fan-out on read).
* ``get_feed()`` merges both sources newest-first and paginates with an opaque cursor.
"""

from __future__ import annotations

import base64
import heapq
import threading
from collections import defaultdict, deque
from dataclasses import dataclass

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock, ValidationError


# --8<-- [start:models]
@dataclass(frozen=True, slots=True)
class Post:
    id: str
    author_id: str
    created_at: float
    content: str

    @property
    def sort_key(self) -> tuple[float, str]:
        """Newest first, ties broken by id so pagination is total and stable."""
        return (self.created_at, self.id)


@dataclass(frozen=True, slots=True)
class FeedPage:
    posts: list[Post]
    next_cursor: str | None


# --8<-- [end:models]


# --8<-- [start:cursor]
def encode_cursor(created_at: float, post_id: str) -> str:
    """Opaque, URL-safe cursor: the client cannot forge page offsets."""
    raw = f"{created_at!r}|{post_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at, post_id = base64.urlsafe_b64decode(padded).decode().split("|", 1)
        return float(created_at), post_id
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("malformed cursor") from exc


# --8<-- [end:cursor]


# --8<-- [start:service]
class FeedService:
    """In-memory stand-in for: graph DB (follows), post store, per-user feed cache (Redis lists)."""

    def __init__(
        self,
        celebrity_threshold: int = 10_000,
        feed_cache_size: int = 800,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._threshold = celebrity_threshold
        self._cache_size = feed_cache_size
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("post")
        self._posts: dict[str, Post] = {}
        self._followers: dict[str, set[str]] = defaultdict(set)  # author -> followers
        self._following: dict[str, set[str]] = defaultdict(set)  # user -> authors
        self._timelines: dict[str, list[str]] = defaultdict(list)  # author -> own post ids
        self._feed_cache: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=self._cache_size))
        self._deleted: set[str] = set()
        self._lock = threading.RLock()

    # -- graph -----------------------------------------------------------------
    def follow(self, follower_id: str, author_id: str) -> None:
        if follower_id == author_id:
            raise ValidationError("users cannot follow themselves")
        with self._lock:
            self._followers[author_id].add(follower_id)
            self._following[follower_id].add(author_id)

    def unfollow(self, follower_id: str, author_id: str) -> None:
        with self._lock:
            self._followers[author_id].discard(follower_id)
            self._following[follower_id].discard(author_id)

    def is_celebrity(self, author_id: str) -> bool:
        return len(self._followers[author_id]) > self._threshold

    # -- write path --------------------------------------------------------------
    def post(self, author_id: str, content: str) -> Post:
        if not content.strip():
            raise ValidationError("empty post")
        with self._lock:
            post = Post(self._ids.next_id(), author_id, self._clock.now(), content)
            self._posts[post.id] = post
            self._timelines[author_id].append(post.id)
            if not self.is_celebrity(author_id):
                # Fan-out on write: O(followers) cache appends, done async by workers in production.
                for follower in self._followers[author_id]:
                    self._feed_cache[follower].append(post.id)
            return post

    def delete_post(self, post_id: str) -> None:
        """Tombstone instead of scrubbing every follower's cache; readers filter lazily."""
        with self._lock:
            if post_id in self._posts:
                self._deleted.add(post_id)

    # -- read path ---------------------------------------------------------------
    def get_feed(self, user_id: str, limit: int = 20, cursor: str | None = None) -> FeedPage:
        if limit <= 0:
            raise ValidationError("limit must be positive")
        boundary = decode_cursor(cursor) if cursor else None
        with self._lock:
            pushed = [self._posts[p] for p in self._feed_cache[user_id]]
            pulled = [
                self._posts[p]
                for author in self._following[user_id]
                if self.is_celebrity(author)
                for p in self._timelines[author][-self._cache_size :]
            ]
            candidates = [
                p
                for p in heapq.merge(
                    sorted(pushed, key=lambda p: p.sort_key, reverse=True),
                    sorted(pulled, key=lambda p: p.sort_key, reverse=True),
                    key=lambda p: p.sort_key,
                    reverse=True,
                )
                if p.id not in self._deleted and (boundary is None or p.sort_key < boundary)
            ]
        page = candidates[:limit]
        next_cursor = encode_cursor(*page[-1].sort_key) if len(candidates) > limit else None
        return FeedPage(page, next_cursor)


# --8<-- [end:service]


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=1_700_000_000)
    feed = FeedService(celebrity_threshold=2, clock=clock)
    for fan in ("ann", "bob", "cat"):
        feed.follow(fan, "star")  # star has 3 followers > threshold 2 -> celebrity
    feed.follow("ann", "dan")  # dan is a normal user
    feed.post("dan", "dan's first post")
    clock.advance(1)
    feed.post("star", "hello from the celebrity")
    clock.advance(1)
    feed.post("dan", "dan again")
    page = feed.get_feed("ann", limit=2)
    for p in page.posts:
        print(f"{p.created_at:.0f} {p.author_id:>4}: {p.content}   (pushed={not feed.is_celebrity(p.author_id)})")
    print("next cursor:", page.next_cursor)
    page2 = feed.get_feed("ann", limit=2, cursor=page.next_cursor)
    print("page 2:", [p.content for p in page2.posts], "| next:", page2.next_cursor)


if __name__ == "__main__":
    main()
