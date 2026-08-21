"""Entities, value objects, enums and domain errors for the Q&A site.

Behaviour lives in ``services.py``; the pluggable rules (reputation, ranking,
search filters) live in ``strategies.py``. The only logic here is the invariant
each object owns: a post body is non-empty, and a post decides which ballots it
accepts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class VoteType(StrEnum):
    UP = "up"
    DOWN = "down"

    @property
    def score_delta(self) -> int:
        """What one ballot of this type contributes to a post score."""
        return 1 if self is VoteType.UP else -1


class PostType(StrEnum):
    QUESTION = "question"
    ANSWER = "answer"
    COMMENT = "comment"


class QuestionStatus(StrEnum):
    OPEN = "open"  # accepting answers
    ANSWERED = "answered"  # has an accepted answer, still accepting more
    CLOSED = "closed"  # duplicate or off-topic: no new answers or comments
    DELETED = "deleted"  # hidden from search and from the front page


class BadgeTier(StrEnum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"


class EventType(StrEnum):
    ANSWER_POSTED = "answer_posted"
    COMMENT_ADDED = "comment_added"
    POST_VOTED = "post_voted"
    ANSWER_ACCEPTED = "answer_accepted"
    QUESTION_CLOSED = "question_closed"


# --8<-- [end:enums]


# --8<-- [start:errors]
class PostNotFoundError(NotFoundError):
    """Unknown post, user or vote id."""


class SelfVoteError(ValidationError):
    """You cannot vote on your own post."""


class DuplicateVoteError(ConflictError):
    """The (voter, post) pair already holds a ballot of this type."""


class VoteNotAllowedError(ValidationError):
    """This post type does not accept this ballot (comments are upvote-only)."""


class QuestionStateError(InvalidStateError):
    """The question's status forbids the operation (answering a closed question)."""


class NotAuthorError(ValidationError):
    """Only the question's author may accept an answer."""


class InsufficientReputationError(ValidationError):
    """The actor lacks the reputation a privilege requires."""


# --8<-- [end:errors]


# --8<-- [start:posts]
@dataclass(slots=True, kw_only=True)
class Post(ABC):
    """Everything a member writes. The base owns votable and commentable behaviour.

    Subclasses declare *what kind* of post they are and *which ballots* they
    accept; no service ever asks ``if isinstance(post, Comment)``.
    """

    id: str
    author_id: str
    body: str
    created_at: float
    score: int = 0
    comment_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.body.strip():
            raise ValidationError("post body must be non-empty")

    @property
    @abstractmethod
    def post_type(self) -> PostType:
        """Discriminator used by the reputation policy and the event stream."""

    @property
    def accepted_vote_types(self) -> tuple[VoteType, ...]:
        """Ballots this post accepts, overridden where the rules differ."""
        return (VoteType.UP, VoteType.DOWN)

    def assert_accepts(self, vote_type: VoteType) -> None:
        if vote_type not in self.accepted_vote_types:
            raise VoteNotAllowedError(f"a {self.post_type} cannot be {vote_type}voted")

    def summary(self) -> str:
        head = self.body.strip().splitlines()[0]
        return head if len(head) <= 48 else head[:45] + "..."


@dataclass(slots=True, kw_only=True)
class Question(Post):
    """The root of a thread: title, tags, answers and one accepted answer."""

    title: str
    tags: frozenset[str] = frozenset()
    status: QuestionStatus = QuestionStatus.OPEN
    accepted_answer_id: str | None = None
    answer_ids: list[str] = field(default_factory=list)
    view_count: int = 0

    post_type = PostType.QUESTION

    def __post_init__(self) -> None:
        # Explicit base call: @dataclass(slots=True) rebuilds the class, which breaks
        # the zero-argument super() closure cell.
        Post.__post_init__(self)
        if not self.title.strip():
            raise ValidationError("question title must be non-empty")

    def accepts_new_posts(self) -> bool:
        return self.status in (QuestionStatus.OPEN, QuestionStatus.ANSWERED)


@dataclass(slots=True, kw_only=True)
class Answer(Post):
    question_id: str
    accepted: bool = False

    post_type = PostType.ANSWER


@dataclass(slots=True, kw_only=True)
class Comment(Post):
    """Attached to a question or an answer. Upvote-only, and never worth reputation."""

    parent_id: str

    post_type = PostType.COMMENT
    accepted_vote_types = (VoteType.UP,)


# --8<-- [end:posts]


# --8<-- [start:values]
@dataclass(frozen=True, slots=True)
class Tag:
    """A normalised topic label. Questions store tag *names*; this is the registry row."""

    name: str
    excerpt: str = ""

    @classmethod
    def of(cls, name: str, excerpt: str = "") -> Tag:
        cleaned = name.strip().lower()
        if not cleaned:
            raise ValidationError("tag name must be non-empty")
        return cls(cleaned, excerpt)


@dataclass(frozen=True, slots=True)
class Vote:
    """One ballot. The (voter_id, post_id) pair is unique across the ledger."""

    id: str
    post_id: str
    voter_id: str
    type: VoteType
    created_at: float


@dataclass(frozen=True, slots=True)
class VoteOutcome:
    """What a ballot did: what it applied, what it replaced, and the score afterwards."""

    vote: Vote | None  # None when the ballot was retracted
    previous: VoteType | None
    score_delta: int
    score: int


@dataclass(frozen=True, slots=True)
class Badge:
    name: str
    tier: BadgeTier
    reason: str


@dataclass(frozen=True, slots=True)
class Bounty:
    """An optional reputation prize attached to a question."""

    id: str
    question_id: str
    sponsor_id: str
    amount: Money
    expires_at: float


@dataclass(slots=True)
class User:
    id: str
    display_name: str
    reputation: int = 1
    badges: list[Badge] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PostEvent:
    """What observers see. Frozen, so a listener cannot corrupt the domain."""

    type: EventType
    at: float
    post_id: str
    post_type: PostType
    actor_id: str  # who did it
    recipient_id: str  # who should hear about it
    value: int = 0  # post score after a vote, or the reputation delta
    detail: str = ""


# --8<-- [end:values]
