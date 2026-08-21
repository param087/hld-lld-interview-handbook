"""One thread end to end: ask, answer, comment, vote, accept, search, close."""

from common import FakeClock, SequentialIdGenerator
from lld.stack_overflow.models import (
    DuplicateVoteError,
    InsufficientReputationError,
    SelfVoteError,
    VoteType,
)
from lld.stack_overflow.services import QnAService, SearchService, build_feed
from lld.stack_overflow.stores import BadgeAwarder, InboxNotifier
from lld.stack_overflow.strategies import HighestScoreRanking, KeywordIn, MinScore, TaggedWith


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    qna = QnAService(clock=clock, ids=SequentialIdGenerator("p"))
    inbox = InboxNotifier()
    qna.subscribe(inbox)
    qna.subscribe(BadgeAwarder(qna))

    asker = qna.register_user("asker")
    expert = qna.register_user("expert")
    crowd = [qna.register_user(f"reader{i}") for i in range(11)]

    question = qna.ask_question(
        asker.id, "Why is my GIL-bound loop slow?", "Threads do not help.", ["python", "concurrency"]
    )
    clock.advance(60)
    answer = qna.post_answer(question.id, expert.id, "Use processes for CPU work.")
    qna.add_comment(answer.id, asker.id, "Works, thanks.")
    print(f"{question.id} {question.title!r} tags={sorted(question.tags)} status={question.status}")

    for reader in crowd:
        qna.cast_vote(answer.id, reader.id, VoteType.UP)
    qna.cast_vote(question.id, expert.id, VoteType.UP)
    print(f"answer score={answer.score}, expert reputation={qna.reputation.reputation(expert.id)}")

    try:
        qna.cast_vote(answer.id, crowd[0].id, VoteType.UP)
    except DuplicateVoteError as exc:
        print(f"double vote rejected: {exc}")
    try:
        qna.cast_vote(answer.id, expert.id, VoteType.UP)
    except SelfVoteError as exc:
        print(f"self vote rejected: {exc}")

    qna.cast_vote(answer.id, crowd[1].id, VoteType.DOWN)  # reader1 switches up -> down
    print(f"reader1 switches: score={answer.score}, expert={qna.reputation.reputation(expert.id)}")

    qna.accept_answer(question.id, answer.id, asker.id)
    print(f"accepted: status={question.status}, expert={qna.reputation.reputation(expert.id)}")
    print(f"badges: expert={[b.name for b in qna.user(expert.id).badges]}, "
          f"asker={[b.name for b in qna.user(asker.id).badges]}")

    other = qna.ask_question(asker.id, "How do I profile Python?", "cProfile output.", ["python"])
    search = SearchService(qna, ranking=HighestScoreRanking())
    for line in build_feed(search.search(TaggedWith("python") & (MinScore(1) | KeywordIn("profile")))):
        print(f"search: {line}")

    try:
        qna.close_question(other.id, expert.id, "needs focus")
    except InsufficientReputationError as exc:
        print(f"close rejected: {exc}")
    qna.reputation.award(expert.id, 3000)  # fast-forward to the close privilege
    qna.close_question(other.id, expert.id, "needs focus")
    print(f"{other.id} closed -> {other.status}; tag counts {qna.tag_counts()}")
    print(f"asker inbox: {inbox.messages(asker.id)[0]}")


if __name__ == "__main__":
    main()
