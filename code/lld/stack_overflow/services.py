"""The Q&A facade and the search service: the only two classes callers touch.

``QnAService`` validates, orders the collaborators in ``stores.py`` and fans out
domain events; ``SearchService`` turns a Specification plus a ranking into a page
of questions. The one lock defined here is the per-question ``RLock``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.stack_overflow.models import (
    Answer,
    Comment,
    EventType,
    NotAuthorError,
    Post,
    PostEvent,
    PostNotFoundError,
    PostType,
    Question,
    QuestionStateError,
    QuestionStatus,
    SelfVoteError,
    Tag,
    User,
    VoteOutcome,
    VoteType,
)
from lld.stack_overflow.stores import (
    PostListener,
    Repository,
    ReputationService,
    VoteLedger,
)
from lld.stack_overflow.strategies import (
    HighestScoreRanking,
    NotSpec,
    QuestionSpec,
    RankingStrategy,
    ReputationPolicy,
    StackOverflowReputation,
    StatusIs,
    TaggedWith,
)


# --8<-- [start:qna]
class QnAService:
    """The facade: posts, ballots, reputation and the observer fan-out in one place.

    Each question owns an ``RLock`` created with the question, so accept, close
    and reopen on the same thread serialise and different threads never see a
    half-applied accept. Lazily creating those locks would itself be a race,
    which is why they are created up front.
    """

    def __init__(
        self,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        policy: ReputationPolicy | None = None,
        user_ids: IdGenerator | None = None,
        vote_ids: IdGenerator | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("p")
        self._user_ids = user_ids or SequentialIdGenerator("u")
        self._users: Repository[User] = Repository("user")
        self._posts: Repository[Post] = Repository("post")
        self._tags: dict[str, Tag] = {}
        self._tag_counts: dict[str, int] = {}
        self._registry_lock = threading.Lock()
        self._question_locks: dict[str, threading.RLock] = {}
        self._listeners: list[PostListener] = []
        self.ledger = VoteLedger(self._clock, vote_ids or SequentialIdGenerator("v"))
        self.reputation = ReputationService(self._users, policy or StackOverflowReputation())

    # -- wiring ---------------------------------------------------------------
    def subscribe(self, listener: PostListener) -> None:
        self._listeners.append(listener)

    def _publish(self, event: PostEvent) -> None:
        for listener in self._listeners:  # outside every lock: a slow listener blocks nobody
            listener.on_event(event)

    def _lock_for(self, question_id: str) -> threading.RLock:
        with self._registry_lock:
            try:
                return self._question_locks[question_id]
            except KeyError:
                raise PostNotFoundError(f"unknown question {question_id!r}") from None

    # -- reads ----------------------------------------------------------------
    def user(self, user_id: str) -> User:
        return self._users.get(user_id)

    def question(self, question_id: str) -> Question:
        return self._typed(question_id, Question)

    def answer(self, answer_id: str) -> Answer:
        return self._typed(answer_id, Answer)

    def post(self, post_id: str) -> Post:
        return self._posts.get(post_id)

    def questions(self) -> list[Question]:
        return [p for p in self._posts.all() if isinstance(p, Question)]

    def tag_counts(self) -> dict[str, int]:
        with self._registry_lock:
            return dict(self._tag_counts)

    def _typed[P: Post](self, post_id: str, kind: type[P]) -> P:
        post = self._posts.get(post_id)
        if not isinstance(post, kind):
            raise PostNotFoundError(f"{post_id!r} is a {post.post_type}, not a {kind.__name__.lower()}")
        return post

    # -- writes ---------------------------------------------------------------
    def register_user(self, display_name: str) -> User:
        user = User(id=self._user_ids.next_id(), display_name=display_name)
        return self._users.add(user.id, user)

    def ask_question(self, author_id: str, title: str, body: str, tags: Iterable[str] = ()) -> Question:
        self._users.get(author_id)
        names = frozenset(Tag.of(name).name for name in tags)
        question = Question(
            id=self._ids.next_id(),
            author_id=author_id,
            body=body,
            created_at=self._clock.now(),
            title=title,
            tags=names,
        )
        with self._registry_lock:
            self._question_locks[question.id] = threading.RLock()
            for name in sorted(names):  # sorted: frozenset order is not stable across runs
                self._tags.setdefault(name, Tag.of(name))
                self._tag_counts[name] = self._tag_counts.get(name, 0) + 1
        return self._posts.add(question.id, question)

    def post_answer(self, question_id: str, author_id: str, body: str) -> Answer:
        self._users.get(author_id)
        question = self.question(question_id)
        with self._lock_for(question_id):
            if not question.accepts_new_posts():
                raise QuestionStateError(f"question {question_id} is {question.status}")
            answer = Answer(
                id=self._ids.next_id(),
                author_id=author_id,
                body=body,
                created_at=self._clock.now(),
                question_id=question_id,
            )
            self._posts.add(answer.id, answer)
            question.answer_ids.append(answer.id)
        self._publish(
            PostEvent(
                type=EventType.ANSWER_POSTED,
                at=self._clock.now(),
                post_id=answer.id,
                post_type=PostType.ANSWER,
                actor_id=author_id,
                recipient_id=question.author_id,
                detail=answer.summary(),
            )
        )
        return answer

    def add_comment(self, parent_id: str, author_id: str, body: str) -> Comment:
        self._users.get(author_id)
        parent = self._posts.get(parent_id)
        question_id = parent.id if isinstance(parent, Question) else self._question_of(parent)
        with self._lock_for(question_id):
            if not self.question(question_id).accepts_new_posts():
                raise QuestionStateError(f"question {question_id} is closed to new comments")
            comment = Comment(
                id=self._ids.next_id(),
                author_id=author_id,
                body=body,
                created_at=self._clock.now(),
                parent_id=parent_id,
            )
            self._posts.add(comment.id, comment)
            parent.comment_ids.append(comment.id)
        self._publish(
            PostEvent(
                type=EventType.COMMENT_ADDED,
                at=self._clock.now(),
                post_id=comment.id,
                post_type=PostType.COMMENT,
                actor_id=author_id,
                recipient_id=parent.author_id,
                detail=comment.summary(),
            )
        )
        return comment

    def _question_of(self, post: Post) -> str:
        if isinstance(post, Answer):
            return post.question_id
        if isinstance(post, Comment):
            return self._question_of(self._posts.get(post.parent_id))
        return post.id

    def cast_vote(self, post_id: str, voter_id: str, vote_type: VoteType) -> VoteOutcome:
        """Validate, claim the ballot, then move reputation. Never the other way round."""
        post = self._posts.get(post_id)
        self._users.get(voter_id)
        if post.author_id == voter_id:
            raise SelfVoteError(f"{voter_id} cannot vote on their own {post.post_type}")
        post.assert_accepts(vote_type)
        outcome = self.ledger.cast(post, voter_id, vote_type)
        self._settle_reputation(post, voter_id, outcome)
        self._publish(
            PostEvent(
                type=EventType.POST_VOTED,
                at=self._clock.now(),
                post_id=post.id,
                post_type=post.post_type,
                actor_id=voter_id,
                recipient_id=post.author_id,
                value=outcome.score,
                detail=f"{vote_type}vote",
            )
        )
        return outcome

    def retract_vote(self, post_id: str, voter_id: str) -> VoteOutcome:
        post = self._posts.get(post_id)
        outcome = self.ledger.retract(post, voter_id)
        self._settle_reputation(post, voter_id, outcome)
        return outcome

    def _settle_reputation(self, post: Post, voter_id: str, outcome: VoteOutcome) -> None:
        """Reverse whatever the previous ballot paid, then apply the new one."""
        policy = self.reputation.policy
        if outcome.previous is not None:
            self.reputation.award(post.author_id, -policy.author_delta(post.post_type, outcome.previous))
            self.reputation.award(voter_id, -policy.voter_delta(post.post_type, outcome.previous))
        if outcome.vote is not None:
            applied = outcome.vote.type
            self.reputation.award(post.author_id, policy.author_delta(post.post_type, applied))
            self.reputation.award(voter_id, policy.voter_delta(post.post_type, applied))

    def accept_answer(self, question_id: str, answer_id: str, actor_id: str) -> None:
        """Only the asker accepts, only one answer is accepted, and switching moves the points."""
        question = self.question(question_id)
        answer = self.answer(answer_id)
        with self._lock_for(question_id):
            if question.status in (QuestionStatus.CLOSED, QuestionStatus.DELETED):
                raise QuestionStateError(f"question {question_id} is {question.status}")
            if question.author_id != actor_id:
                raise NotAuthorError(f"{actor_id} did not ask {question_id}")
            if answer.question_id != question_id:
                raise QuestionStateError(f"answer {answer_id} does not belong to {question_id}")
            if question.accepted_answer_id == answer_id:
                return  # idempotent: a double click is not an error
            answer_points, giver_points = self.reputation.policy.accept_delta()
            if question.accepted_answer_id is None:
                self.reputation.award(actor_id, giver_points)
            else:
                previous = self.answer(question.accepted_answer_id)
                previous.accepted = False
                self.reputation.award(previous.author_id, -self._accept_points(previous, actor_id, answer_points))
            answer.accepted = True
            question.accepted_answer_id = answer_id
            question.status = QuestionStatus.ANSWERED
            self.reputation.award(answer.author_id, self._accept_points(answer, actor_id, answer_points))
        self._publish(
            PostEvent(
                type=EventType.ANSWER_ACCEPTED,
                at=self._clock.now(),
                post_id=answer_id,
                post_type=PostType.ANSWER,
                actor_id=actor_id,
                recipient_id=answer.author_id,
                value=answer_points,
            )
        )

    @staticmethod
    def _accept_points(answer: Answer, actor_id: str, points: int) -> int:
        """Accepting your own answer earns nothing — otherwise reputation is free."""
        return 0 if answer.author_id == actor_id else points

    def close_question(self, question_id: str, moderator_id: str, reason: str) -> None:
        question = self.question(question_id)
        self.reputation.require(moderator_id, self.reputation.policy.close_threshold(), "closing a question")
        with self._lock_for(question_id):
            if question.status is QuestionStatus.DELETED:
                raise QuestionStateError(f"question {question_id} is deleted")
            question.status = QuestionStatus.CLOSED
        self._publish(
            PostEvent(
                type=EventType.QUESTION_CLOSED,
                at=self._clock.now(),
                post_id=question_id,
                post_type=PostType.QUESTION,
                actor_id=moderator_id,
                recipient_id=question.author_id,
                detail=reason,
            )
        )

    def reopen_question(self, question_id: str, moderator_id: str) -> None:
        question = self.question(question_id)
        self.reputation.require(moderator_id, self.reputation.policy.close_threshold(), "reopening a question")
        with self._lock_for(question_id):
            if question.status is not QuestionStatus.CLOSED:
                raise QuestionStateError(f"question {question_id} is {question.status}, not closed")
            question.status = (
                QuestionStatus.ANSWERED if question.accepted_answer_id else QuestionStatus.OPEN
            )

    def delete_question(self, question_id: str, actor_id: str) -> None:
        question = self.question(question_id)
        with self._lock_for(question_id):
            if question.author_id != actor_id:
                self.reputation.require(actor_id, self.reputation.policy.close_threshold(), "deleting")
            question.status = QuestionStatus.DELETED


# --8<-- [end:qna]


# --8<-- [start:search]
class SearchService:
    """Specification in, ranked page out. It never grows an ``if`` per filter."""

    def __init__(self, qna: QnAService, ranking: RankingStrategy | None = None) -> None:
        self._qna = qna
        self._ranking = ranking or HighestScoreRanking()

    def search(
        self,
        spec: QuestionSpec,
        ranking: RankingStrategy | None = None,
        limit: int = 10,
    ) -> list[Question]:
        visible = spec & NotSpec(StatusIs(QuestionStatus.DELETED))
        matches = [q for q in self._qna.questions() if visible.is_satisfied_by(q)]
        return (ranking or self._ranking).rank(matches)[:limit]

    def by_tag(self, tag: str, limit: int = 10) -> list[Question]:
        return self.search(TaggedWith(tag), limit=limit)


# --8<-- [end:search]


def build_feed(questions: Sequence[Question]) -> list[str]:
    """Render a ranked list the way the front page would."""
    return [f"[{q.score:+d}] {q.title} ({', '.join(sorted(q.tags))}) - {q.status}" for q in questions]
