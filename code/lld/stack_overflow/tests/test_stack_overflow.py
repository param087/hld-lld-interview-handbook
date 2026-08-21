from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, SequentialIdGenerator
from lld.stack_overflow.models import (
    DuplicateVoteError,
    InsufficientReputationError,
    NotAuthorError,
    PostNotFoundError,
    QuestionStateError,
    QuestionStatus,
    SelfVoteError,
    VoteNotAllowedError,
    VoteType,
)
from lld.stack_overflow.services import QnAService, SearchService
from lld.stack_overflow.stores import BadgeAwarder, InboxNotifier
from lld.stack_overflow.strategies import (
    AskedBy,
    FlatReputation,
    HasAcceptedAnswer,
    HighestScoreRanking,
    KeywordIn,
    MinScore,
    MostAnsweredRanking,
    NewestFirstRanking,
    StatusIs,
    TaggedWith,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000)


def make_service(clock: FakeClock, policy=None) -> QnAService:
    return QnAService(
        clock=clock,
        ids=SequentialIdGenerator("p"),
        policy=policy,
        user_ids=SequentialIdGenerator("u"),
        vote_ids=SequentialIdGenerator("v"),
    )


def test_ask_answer_upvote_and_accept_moves_reputation(clock: FakeClock) -> None:
    qna = make_service(clock)
    asker, expert, reader = (qna.register_user(n) for n in ("asker", "expert", "reader"))
    question = qna.ask_question(asker.id, "Why?", "Because.", ["python"])
    answer = qna.post_answer(question.id, expert.id, "Do this.")

    qna.cast_vote(answer.id, reader.id, VoteType.UP)
    assert answer.score == 1 and qna.reputation.reputation(expert.id) == 11  # 1 + 10

    qna.accept_answer(question.id, answer.id, asker.id)
    assert question.status is QuestionStatus.ANSWERED and answer.accepted
    assert qna.reputation.reputation(expert.id) == 26  # 11 + 15 for the accept
    assert qna.reputation.reputation(asker.id) == 3  # 1 + 2 for accepting


@pytest.mark.parametrize(
    ("first", "second", "expected_score", "expected_author_reputation"),
    [
        (VoteType.UP, VoteType.DOWN, -1, 1),  # 1 + 10, then -10 -2 -> floored at 1
        # The floor is lossy: 1 - 2 clamps to 1, so undoing it hands back 2 you never paid.
        (VoteType.DOWN, VoteType.UP, 1, 13),  # 1 (clamped), then +2 +10
    ],
)
def test_switching_a_ballot_reverses_the_previous_one(
    clock: FakeClock, first: VoteType, second: VoteType, expected_score: int, expected_author_reputation: int
) -> None:
    qna = make_service(clock)
    asker, expert, reader = (qna.register_user(n) for n in ("asker", "expert", "reader"))
    question = qna.ask_question(asker.id, "Why?", "Because.")
    answer = qna.post_answer(question.id, expert.id, "Do this.")
    qna.cast_vote(answer.id, reader.id, first)
    outcome = qna.cast_vote(answer.id, reader.id, second)
    assert outcome.previous is first and answer.score == expected_score
    assert qna.reputation.reputation(expert.id) == expected_author_reputation


def test_validation_rules_reject_self_votes_duplicates_and_comment_downvotes(clock: FakeClock) -> None:
    qna = make_service(clock)
    asker, reader = qna.register_user("asker"), qna.register_user("reader")
    question = qna.ask_question(asker.id, "Why?", "Because.")
    comment = qna.add_comment(question.id, reader.id, "Nice question.")

    with pytest.raises(SelfVoteError):
        qna.cast_vote(question.id, asker.id, VoteType.UP)
    with pytest.raises(VoteNotAllowedError):
        qna.cast_vote(comment.id, asker.id, VoteType.DOWN)
    qna.cast_vote(question.id, reader.id, VoteType.UP)
    with pytest.raises(DuplicateVoteError):
        qna.cast_vote(question.id, reader.id, VoteType.UP)
    with pytest.raises(PostNotFoundError):
        qna.cast_vote("nope", reader.id, VoteType.UP)


def test_question_lifecycle_open_answered_closed_reopened(clock: FakeClock) -> None:
    qna = make_service(clock)
    asker, expert = qna.register_user("asker"), qna.register_user("expert")
    qna.reputation.award(expert.id, 3000)
    question = qna.ask_question(asker.id, "Why?", "Because.")
    answer = qna.post_answer(question.id, expert.id, "Do this.")

    qna.accept_answer(question.id, answer.id, asker.id)
    assert question.status is QuestionStatus.ANSWERED

    qna.close_question(question.id, expert.id, "duplicate")
    assert question.status is QuestionStatus.CLOSED
    with pytest.raises(QuestionStateError):
        qna.post_answer(question.id, expert.id, "too late")
    with pytest.raises(InsufficientReputationError):
        qna.reopen_question(question.id, asker.id)

    qna.reopen_question(question.id, expert.id)
    assert question.status is QuestionStatus.ANSWERED  # it still has an accepted answer


def test_only_the_asker_accepts_and_switching_moves_the_points(clock: FakeClock) -> None:
    qna = make_service(clock)
    asker, first, second = (qna.register_user(n) for n in ("asker", "first", "second"))
    question = qna.ask_question(asker.id, "Why?", "Because.")
    answer_a = qna.post_answer(question.id, first.id, "Older answer.")
    answer_b = qna.post_answer(question.id, second.id, "Better answer.")

    with pytest.raises(NotAuthorError):
        qna.accept_answer(question.id, answer_a.id, first.id)

    qna.accept_answer(question.id, answer_a.id, asker.id)
    qna.accept_answer(question.id, answer_a.id, asker.id)  # idempotent
    assert qna.reputation.reputation(first.id) == 16 and qna.reputation.reputation(asker.id) == 3

    qna.accept_answer(question.id, answer_b.id, asker.id)
    assert not answer_a.accepted and answer_b.accepted
    assert qna.reputation.reputation(first.id) == 1  # 16 - 15, floored at the minimum
    assert qna.reputation.reputation(second.id) == 16
    assert qna.reputation.reputation(asker.id) == 3  # the +2 is paid once, not per accept


# --8<-- [start:concurrency]
def test_concurrent_ballots_respect_the_uniqueness_constraint(clock: FakeClock) -> None:
    """Eight readers, three clicks each, all at once: eight ballots and a score of 8."""
    qna = make_service(clock)
    asker, expert = qna.register_user("asker"), qna.register_user("expert")
    question = qna.ask_question(asker.id, "Why?", "Because.")
    answer = qna.post_answer(question.id, expert.id, "Do this.")
    readers = [qna.register_user(f"reader{i}") for i in range(8)]

    def click(attempt: int) -> bool:
        try:
            qna.cast_vote(answer.id, readers[attempt % 8].id, VoteType.UP)
        except DuplicateVoteError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(click, range(24)))

    assert results.count(True) == 8 and results.count(False) == 16
    assert answer.score == 8 and len(qna.ledger.ballots_for(answer.id)) == 8
    assert qna.reputation.reputation(expert.id) == 81  # 1 + 8 x 10, no lost updates


# --8<-- [end:concurrency]


def test_retracting_a_ballot_undoes_the_score_and_the_reputation(clock: FakeClock) -> None:
    qna = make_service(clock)
    asker, expert, reader = (qna.register_user(n) for n in ("asker", "expert", "reader"))
    question = qna.ask_question(asker.id, "Why?", "Because.")
    answer = qna.post_answer(question.id, expert.id, "Do this.")
    qna.cast_vote(answer.id, reader.id, VoteType.DOWN)
    assert qna.reputation.reputation(reader.id) == 1  # 1 - 1 for downvoting, floored

    outcome = qna.retract_vote(answer.id, reader.id)
    assert outcome.vote is None and outcome.previous is VoteType.DOWN and answer.score == 0
    assert qna.ledger.vote_of(answer.id, reader.id) is None
    with pytest.raises(PostNotFoundError):
        qna.retract_vote(answer.id, reader.id)


# --8<-- [start:search]
def test_search_composes_specifications_and_honours_the_ranking(clock: FakeClock) -> None:
    qna = make_service(clock)
    asker, reader = qna.register_user("asker"), qna.register_user("reader")
    slow = qna.ask_question(asker.id, "Why is threading slow?", "GIL.", ["python", "threads"])
    clock.advance(60)
    fast = qna.ask_question(asker.id, "How to profile Python?", "cProfile.", ["python"])
    clock.advance(60)
    other = qna.ask_question(asker.id, "Rust borrow checker?", "Lifetimes.", ["rust"])
    qna.cast_vote(fast.id, reader.id, VoteType.UP)

    search = SearchService(qna)
    python_only = TaggedWith("python")
    assert [q.id for q in search.search(python_only, HighestScoreRanking())] == [fast.id, slow.id]
    assert [q.id for q in search.search(python_only, NewestFirstRanking())] == [fast.id, slow.id]
    assert [q.id for q in search.search(TaggedWith("python") & KeywordIn("threading"))] == [slow.id]
    assert [q.id for q in search.search(TaggedWith("rust") | MinScore(1))] == [fast.id, other.id]
    assert search.search(HasAcceptedAnswer()) == []
    assert [q.id for q in search.by_tag("rust")] == [other.id]

    qna.delete_question(other.id, asker.id)
    assert search.search(AskedBy(asker.id), MostAnsweredRanking()) and other not in search.search(
        AskedBy(asker.id)
    )
    assert [q.id for q in search.search(StatusIs(QuestionStatus.OPEN))] == [fast.id, slow.id]


# --8<-- [end:search]


def test_observers_fill_the_inbox_and_award_each_badge_once(clock: FakeClock) -> None:
    qna = make_service(clock)
    inbox = InboxNotifier()
    qna.subscribe(inbox)
    qna.subscribe(BadgeAwarder(qna))
    asker, expert = qna.register_user("asker"), qna.register_user("expert")
    question = qna.ask_question(asker.id, "Why?", "Because.")
    answer = qna.post_answer(question.id, expert.id, "Do this.")
    for i in range(12):
        qna.cast_vote(answer.id, qna.register_user(f"r{i}").id, VoteType.UP)

    assert [b.name for b in qna.user(expert.id).badges] == ["Nice Answer"]  # once, not three times
    qna.accept_answer(question.id, answer.id, asker.id)
    qna.accept_answer(question.id, answer.id, asker.id)
    assert [b.name for b in qna.user(asker.id).badges] == ["Scholar"]
    assert inbox.messages(asker.id)[0].startswith("answer_posted")
    assert inbox.messages(expert.id) and not any(
        m.startswith("answer_posted") for m in inbox.messages(expert.id)
    )


def test_a_different_reputation_policy_changes_every_number(clock: FakeClock) -> None:
    qna = make_service(clock, policy=FlatReputation(points=1))
    asker, expert, reader = (qna.register_user(n) for n in ("asker", "expert", "reader"))
    question = qna.ask_question(asker.id, "Why?", "Because.")
    answer = qna.post_answer(question.id, expert.id, "Do this.")
    qna.cast_vote(answer.id, reader.id, VoteType.UP)
    qna.accept_answer(question.id, answer.id, asker.id)
    assert qna.reputation.reputation(expert.id) == 3  # 1 + 1 + 1
    assert qna.reputation.reputation(asker.id) == 1  # no accepter bonus in this policy
    qna.close_question(question.id, reader.id, "off topic")  # no privilege threshold either
    assert question.status is QuestionStatus.CLOSED


def test_accepting_your_own_answer_earns_nothing(clock: FakeClock) -> None:
    qna = make_service(clock)
    asker = qna.register_user("asker")
    question = qna.ask_question(asker.id, "Why?", "Because.")
    answer = qna.post_answer(question.id, asker.id, "I found it.")
    qna.accept_answer(question.id, answer.id, asker.id)
    assert qna.reputation.reputation(asker.id) == 3  # only the +2 for accepting
    assert question.status is QuestionStatus.ANSWERED
