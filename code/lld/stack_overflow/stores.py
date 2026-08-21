"""The collaborators every service leans on, and every lock in the system.

Read them in this order: the repository seam, the ledger that owns the
``(voter, post)`` uniqueness constraint, the reputation column, then the two
observers. ``services.py`` orchestrates these; it adds only the per-question
lock.
"""

from __future__ import annotations

import threading
from typing import Protocol

from common import Clock, IdGenerator
from lld.stack_overflow.models import (
    Badge,
    BadgeTier,
    DuplicateVoteError,
    EventType,
    InsufficientReputationError,
    Post,
    PostEvent,
    PostNotFoundError,
    PostType,
    User,
    Vote,
    VoteOutcome,
    VoteType,
)
from lld.stack_overflow.strategies import ReputationPolicy


# --8<-- [start:repository]
class Repository[T]:
    """The persistence seam: one dict today, one table tomorrow.

    Services depend on this class, never on a dict, so swapping in SQL is a
    constructor change. The lock guards the index only; the entities it hands
    out are protected by the ledger and the per-question locks below.
    """

    def __init__(self, entity: str) -> None:
        self._entity = entity
        self._items: dict[str, T] = {}
        self._lock = threading.Lock()

    def add(self, key: str, item: T) -> T:
        with self._lock:
            self._items[key] = item
        return item

    def get(self, key: str) -> T:
        with self._lock:
            try:
                return self._items[key]
            except KeyError:
                raise PostNotFoundError(f"unknown {self._entity} {key!r}") from None

    def find(self, key: str) -> T | None:
        with self._lock:
            return self._items.get(key)

    def all(self) -> list[T]:
        with self._lock:
            return list(self._items.values())


# --8<-- [end:repository]


# --8<-- [start:ledger]
class VoteLedger:
    """The (voter, post) uniqueness constraint, and the score it keeps in step.

    Ballots are sharded by post id. Every ballot for a post lands in one shard
    and is only ever touched while that shard's lock is held, so the row and
    ``post.score`` move together and two threads can never both insert the
    "first" ballot for the same pair. Posts in different shards do not contend.
    """

    SHARDS = 8

    def __init__(self, clock: Clock, ids: IdGenerator) -> None:
        self._clock = clock
        self._ids = ids
        self._shards: tuple[dict[tuple[str, str], Vote], ...] = tuple({} for _ in range(self.SHARDS))
        self._locks = tuple(threading.Lock() for _ in range(self.SHARDS))

    def _index(self, post_id: str) -> int:
        return hash(post_id) % self.SHARDS

    def cast(self, post: Post, voter_id: str, vote_type: VoteType) -> VoteOutcome:
        """Insert or switch a ballot. Casting the same type twice is a conflict."""
        index = self._index(post.id)
        key = (voter_id, post.id)
        with self._locks[index]:
            shard = self._shards[index]
            previous = shard.get(key)
            if previous is not None and previous.type is vote_type:
                raise DuplicateVoteError(f"{voter_id} already {vote_type}voted {post.id}")
            vote = Vote(
                id=self._ids.next_id(),
                post_id=post.id,
                voter_id=voter_id,
                type=vote_type,
                created_at=self._clock.now(),
            )
            delta = vote_type.score_delta - (previous.type.score_delta if previous else 0)
            shard[key] = vote
            post.score += delta
            return VoteOutcome(vote, previous.type if previous else None, delta, post.score)

    def retract(self, post: Post, voter_id: str) -> VoteOutcome:
        """Undo a ballot. The stored row *is* the undo record; no command log needed."""
        index = self._index(post.id)
        key = (voter_id, post.id)
        with self._locks[index]:
            shard = self._shards[index]
            previous = shard.pop(key, None)
            if previous is None:
                raise PostNotFoundError(f"{voter_id} has no ballot on {post.id}")
            delta = -previous.type.score_delta
            post.score += delta
            return VoteOutcome(None, previous.type, delta, post.score)

    def vote_of(self, post_id: str, voter_id: str) -> Vote | None:
        index = self._index(post_id)
        with self._locks[index]:
            return self._shards[index].get((voter_id, post_id))

    def ballots_for(self, post_id: str) -> list[Vote]:
        index = self._index(post_id)
        with self._locks[index]:
            return [v for (_, pid), v in self._shards[index].items() if pid == post_id]


# --8<-- [end:ledger]


# --8<-- [start:reputation]
class ReputationService:
    """Owns the reputation column and the floor under it.

    Lock order rule: this lock is always taken *after* a ballot shard lock or a
    question lock and never the other way round, so the two can never deadlock.
    """

    MINIMUM = 1

    def __init__(self, users: Repository[User], policy: ReputationPolicy) -> None:
        self._users = users
        self._policy = policy
        self._lock = threading.Lock()

    @property
    def policy(self) -> ReputationPolicy:
        return self._policy

    def award(self, user_id: str, delta: int) -> int:
        if delta == 0:
            return self.reputation(user_id)
        user = self._users.get(user_id)
        with self._lock:
            user.reputation = max(self.MINIMUM, user.reputation + delta)
            return user.reputation

    def reputation(self, user_id: str) -> int:
        return self._users.get(user_id).reputation

    def require(self, user_id: str, needed: int, privilege: str) -> None:
        have = self.reputation(user_id)
        if have < needed:
            raise InsufficientReputationError(
                f"{privilege} needs {needed} reputation, {user_id} has {have}"
            )


# --8<-- [end:reputation]


# --8<-- [start:observers]
class PostListener(Protocol):
    """Observer interface. Listeners are called outside every domain lock."""

    def on_event(self, event: PostEvent) -> None: ...


class InboxNotifier:
    """The bell icon: one message list per recipient, newest last."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inbox: dict[str, list[str]] = {}

    def on_event(self, event: PostEvent) -> None:
        if event.actor_id == event.recipient_id:
            return  # you do not get notified about your own action
        text = f"{event.type} on {event.post_id} by {event.actor_id}"
        if event.detail:
            text = f"{text}: {event.detail}"
        with self._lock:
            self._inbox.setdefault(event.recipient_id, []).append(text)

    def messages(self, user_id: str) -> list[str]:
        with self._lock:
            return list(self._inbox.get(user_id, ()))


class UserLookup(Protocol):
    """The narrow slice of the facade a badge rule needs."""

    def user(self, user_id: str) -> User: ...


class BadgeAwarder:
    """Awards a badge at most once per (user, badge), whatever the event storm."""

    NICE_ANSWER_SCORE = 10

    def __init__(self, users: UserLookup) -> None:
        self._users = users
        self._lock = threading.Lock()
        self._granted: set[tuple[str, str]] = set()

    def on_event(self, event: PostEvent) -> None:
        badge: Badge | None = None
        if (
            event.type is EventType.POST_VOTED
            and event.post_type is PostType.ANSWER
            and event.value >= self.NICE_ANSWER_SCORE
        ):
            badge = Badge("Nice Answer", BadgeTier.BRONZE, f"answer {event.post_id} reached +10")
        elif event.type is EventType.ANSWER_ACCEPTED:
            badge = Badge("Scholar", BadgeTier.BRONZE, "accepted an answer")
        if badge is None:
            return
        owner = event.actor_id if event.type is EventType.ANSWER_ACCEPTED else event.recipient_id
        with self._lock:
            if (owner, badge.name) in self._granted:
                return
            self._granted.add((owner, badge.name))
        self._users.user(owner).badges.append(badge)


# --8<-- [end:observers]
