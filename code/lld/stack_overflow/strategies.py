"""The three rules the interviewer will ask you to change: reputation, ranking, search.

Each is an interface plus small implementations, so "now make downvotes free" or
"now sort by activity" is a new class, never an edit to ``QnAService``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Protocol

from lld.stack_overflow.models import PostType, Question, QuestionStatus, VoteType


# --8<-- [start:reputation]
class ReputationPolicy(Protocol):
    """How ballots and accepts turn into reputation.

    Deltas are quoted per ballot, never per transition: the service composes a
    switch (up to down) as ``-delta(up) + delta(down)`` and a retraction as
    ``-delta(previous)``, so a policy never has to know about history.
    """

    def author_delta(self, post_type: PostType, vote_type: VoteType) -> int:
        """Reputation the post's author gains from one ballot."""
        ...

    def voter_delta(self, post_type: PostType, vote_type: VoteType) -> int:
        """Reputation the voter pays for casting one ballot."""
        ...

    def accept_delta(self) -> tuple[int, int]:
        """``(answer author, accepter)`` reputation on the first accept."""
        ...

    def close_threshold(self) -> int:
        """Reputation required to close or reopen a question."""
        ...


class StackOverflowReputation:
    """The published rules: +10 for a useful answer, -2 for a bad one, -1 to downvote."""

    UPVOTE_ANSWER = 10
    UPVOTE_QUESTION = 5
    DOWNVOTE_AUTHOR = -2
    DOWNVOTE_COST = -1
    ACCEPT_ANSWER = 15
    ACCEPT_GIVER = 2
    CLOSE_PRIVILEGE = 3000

    def author_delta(self, post_type: PostType, vote_type: VoteType) -> int:
        if post_type is PostType.COMMENT:
            return 0  # comment votes are cosmetic
        if vote_type is VoteType.DOWN:
            return self.DOWNVOTE_AUTHOR
        return self.UPVOTE_ANSWER if post_type is PostType.ANSWER else self.UPVOTE_QUESTION

    def voter_delta(self, post_type: PostType, vote_type: VoteType) -> int:
        # Downvoting an answer costs you something; downvoting a question is free.
        if vote_type is VoteType.DOWN and post_type is PostType.ANSWER:
            return self.DOWNVOTE_COST
        return 0

    def accept_delta(self) -> tuple[int, int]:
        return self.ACCEPT_ANSWER, self.ACCEPT_GIVER

    def close_threshold(self) -> int:
        return self.CLOSE_PRIVILEGE


class FlatReputation:
    """Every ballot is worth the same, votes are free, nobody needs a privilege.

    The policy you hand a new community. It exists to prove the seam: swapping it
    changes every number on the site and touches no service code.
    """

    def __init__(self, points: int = 1) -> None:
        self._points = points

    def author_delta(self, post_type: PostType, vote_type: VoteType) -> int:
        return self._points * vote_type.score_delta

    def voter_delta(self, post_type: PostType, vote_type: VoteType) -> int:
        return 0

    def accept_delta(self) -> tuple[int, int]:
        return self._points, 0

    def close_threshold(self) -> int:
        return 0


# --8<-- [end:reputation]


# --8<-- [start:ranking]
class RankingStrategy(Protocol):
    """Orders a result set. Stable: ties keep insertion order."""

    def rank(self, questions: Sequence[Question]) -> list[Question]: ...


class NewestFirstRanking:
    def rank(self, questions: Sequence[Question]) -> list[Question]:
        return sorted(questions, key=lambda q: -q.created_at)


class HighestScoreRanking:
    def rank(self, questions: Sequence[Question]) -> list[Question]:
        return sorted(questions, key=lambda q: (-q.score, -q.created_at))


class MostAnsweredRanking:
    def rank(self, questions: Sequence[Question]) -> list[Question]:
        return sorted(questions, key=lambda q: (-len(q.answer_ids), -q.score))


# --8<-- [end:ranking]


# --8<-- [start:specification]
class QuestionSpec(ABC):
    """A composable yes/no rule about one question.

    An ``ABC`` rather than a ``Protocol`` because ``&``, ``|`` and ``~`` are
    shared behaviour every leaf inherits for free. The filter is data: you can
    print it, log it, or later translate it into SQL instead of evaluating it.
    """

    @abstractmethod
    def is_satisfied_by(self, question: Question) -> bool: ...

    @abstractmethod
    def describe(self) -> str: ...

    def __and__(self, other: QuestionSpec) -> QuestionSpec:
        return AndSpec(self, other)

    def __or__(self, other: QuestionSpec) -> QuestionSpec:
        return OrSpec(self, other)

    def __invert__(self) -> QuestionSpec:
        return NotSpec(self)


class AndSpec(QuestionSpec):
    def __init__(self, left: QuestionSpec, right: QuestionSpec) -> None:
        self._left, self._right = left, right

    def is_satisfied_by(self, question: Question) -> bool:
        return self._left.is_satisfied_by(question) and self._right.is_satisfied_by(question)

    def describe(self) -> str:
        return f"({self._left.describe()} AND {self._right.describe()})"


class OrSpec(QuestionSpec):
    def __init__(self, left: QuestionSpec, right: QuestionSpec) -> None:
        self._left, self._right = left, right

    def is_satisfied_by(self, question: Question) -> bool:
        return self._left.is_satisfied_by(question) or self._right.is_satisfied_by(question)

    def describe(self) -> str:
        return f"({self._left.describe()} OR {self._right.describe()})"


class NotSpec(QuestionSpec):
    def __init__(self, inner: QuestionSpec) -> None:
        self._inner = inner

    def is_satisfied_by(self, question: Question) -> bool:
        return not self._inner.is_satisfied_by(question)

    def describe(self) -> str:
        return f"NOT {self._inner.describe()}"


class TaggedWith(QuestionSpec):
    def __init__(self, tag: str) -> None:
        self._tag = tag.strip().lower()

    def is_satisfied_by(self, question: Question) -> bool:
        return self._tag in question.tags

    def describe(self) -> str:
        return f"tag={self._tag}"


class KeywordIn(QuestionSpec):
    """Case-insensitive substring match over title and body — the in-memory stand-in
    for an inverted index. The Specification hides which one you use."""

    def __init__(self, term: str) -> None:
        self._term = term.strip().lower()

    def is_satisfied_by(self, question: Question) -> bool:
        return self._term in question.title.lower() or self._term in question.body.lower()

    def describe(self) -> str:
        return f"keyword={self._term!r}"


class MinScore(QuestionSpec):
    def __init__(self, minimum: int) -> None:
        self._minimum = minimum

    def is_satisfied_by(self, question: Question) -> bool:
        return question.score >= self._minimum

    def describe(self) -> str:
        return f"score>={self._minimum}"


class HasAcceptedAnswer(QuestionSpec):
    def is_satisfied_by(self, question: Question) -> bool:
        return question.accepted_answer_id is not None

    def describe(self) -> str:
        return "accepted=yes"


class AskedBy(QuestionSpec):
    def __init__(self, author_id: str) -> None:
        self._author_id = author_id

    def is_satisfied_by(self, question: Question) -> bool:
        return question.author_id == self._author_id

    def describe(self) -> str:
        return f"author={self._author_id}"


class StatusIs(QuestionSpec):
    def __init__(self, status: QuestionStatus) -> None:
        self._status = status

    def is_satisfied_by(self, question: Question) -> bool:
        return question.status is self._status

    def describe(self) -> str:
        return f"status={self._status}"


# --8<-- [end:specification]
