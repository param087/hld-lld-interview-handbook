from concurrent.futures import ThreadPoolExecutor

import pytest

from common import NotFoundError, ValidationError
from hld.email_threading import MailboxThreader, Message, UnionFind, normalise_subject


def reply(mid: str, parent: str, at: float, *, subject: str = "Re: Ship the release") -> Message:
    return Message(mid, f"{mid[1]}@corp.example", subject, at, in_reply_to=parent,
                   references=(parent,))


def test_reply_joins_the_thread_of_its_parent() -> None:
    index = MailboxThreader()
    root = Message("<a@x>", "ana@corp.example", "Ship the release", 1.0)
    index.add(root)
    assert index.add(reply("<b@x>", "<a@x>", 2.0)) == "<a@x>"
    thread = index.thread_of("<b@x>")
    assert thread.message_ids == ("<a@x>", "<b@x>")
    assert thread.subject == "Ship the release"  # the oldest message names the thread
    assert thread.participants == ("ana@corp.example", "b@corp.example")
    assert thread.updated_at == 2.0


def test_out_of_order_delivery_creates_a_ghost_that_a_later_message_fills_in() -> None:
    index = MailboxThreader()
    # Cara replies to Bob's mail; this mailbox was copied on the reply, not the original.
    cara = Message("<c@x>", "cara@corp.example", "Re: Ship the release", 3.0,
                   in_reply_to="<b@x>", references=("<a@x>", "<b@x>"))
    index.add(cara)
    assert index.ghost_ids() == {"<a@x>", "<b@x>"}
    assert index.thread_of("<c@x>").size == 1  # ghosts are not rendered
    index.add(Message("<a@x>", "ana@corp.example", "Ship the release", 1.0))
    index.add(reply("<b@x>", "<a@x>", 2.0))
    assert index.ghost_ids() == set()
    assert index.thread_of("<c@x>").message_ids == ("<a@x>", "<b@x>", "<c@x>")


def test_stripped_headers_are_rescued_by_the_normalised_subject() -> None:
    index = MailboxThreader()
    index.add(Message("<a@x>", "ana@corp.example", "Ship the release", 1.0))
    orphan = Message("<d@x>", "dan@corp.example", "RE: Ship the release", 4.0)
    assert index.add(orphan) == "<a@x>"
    assert index.thread_of("<d@x>").size == 2


def test_subject_fallback_never_merges_two_fresh_messages() -> None:
    strict = MailboxThreader(merge_by_subject=False)
    strict.add(Message("<a@x>", "ana@corp.example", "Ship the release", 1.0))
    assert strict.add(Message("<d@x>", "dan@corp.example", "RE: Ship the release", 4.0)) == "<d@x>"

    loose = MailboxThreader()
    loose.add(Message("<p@x>", "pat@corp.example", "Lunch", 1.0))
    # No Re:, no References -> a different conversation that happens to share a subject.
    assert loose.add(Message("<q@x>", "quinn@corp.example", "Lunch", 2.0)) == "<q@x>"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Re: Re: Fwd: Ship it", "ship it"),
        ("RE[2]: Ship it", "ship it"),
        ("  Fw: SHIP IT  ", "ship it"),
        ("Ship it", "ship it"),
        ("Re:", ""),
    ],
)
def test_normalise_subject_strips_every_prefix_run(raw: str, expected: str) -> None:
    assert normalise_subject(raw) == expected


def test_redelivery_is_idempotent_because_smtp_is_at_least_once() -> None:
    index = MailboxThreader()
    original = Message("<a@x>", "ana@corp.example", "Ship the release", 1.0)
    index.add(original)
    index.add(reply("<b@x>", "<a@x>", 2.0))
    for _ in range(3):
        assert index.add(original) == "<a@x>"
    assert index.thread_of("<a@x>").message_ids == ("<a@x>", "<b@x>")


def test_union_by_size_keeps_the_larger_root() -> None:
    sets = UnionFind()
    for mid in ("m1", "m2", "m3"):
        sets.union("big", mid)
    sets.add("small")
    assert sets.union("small", "m2") == "big"  # the 4-member set survives, 1 id is rewritten
    assert sorted(sets.members("small")) == ["big", "m1", "m2", "m3", "small"]


def test_validation_and_lookup_errors() -> None:
    index = MailboxThreader()
    with pytest.raises(ValidationError):
        index.add(Message("   ", "ana@corp.example", "Ship the release", 1.0))
    with pytest.raises(NotFoundError):
        index.thread_of("<never-delivered@x>")


def test_concurrent_deliveries_land_in_one_thread() -> None:
    index = MailboxThreader()
    index.add(Message("<a@x>", "ana@corp.example", "Ship the release", 1.0))
    with ThreadPoolExecutor(max_workers=8) as pool:
        roots = list(pool.map(lambda i: index.add(reply(f"<r{i}@x>", "<a@x>", 2.0 + i)), range(200)))
    assert set(roots) == {"<a@x>"}
    assert index.thread_of("<a@x>").size == 201
    assert len(index.threads()) == 1
