import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import InvalidStateError, ValidationError
from hld.ot_transform import (
    ClientDocument,
    Delete,
    DocumentServer,
    Insert,
    Op,
    apply_op,
    transform,
)

DOC = "hello world"


def both_orders(doc: str, first: Op, second: Op) -> tuple[str, str]:
    """Apply the two concurrent ops in both orders, transforming the second one each time.

    ``first`` wins every tie, which is the convention the server uses: the op already in the log
    stays put and the newcomer shifts. Returning both strings lets a test assert convergence.
    """
    left = apply_op(doc, first)
    rebased_second = transform(second, first, op_first=False)
    if rebased_second is not None:
        left = apply_op(left, rebased_second)
    right = apply_op(doc, second)
    rebased_first = transform(first, second, op_first=True)
    if rebased_first is not None:
        right = apply_op(right, rebased_first)
    return left, right


def test_concurrent_inserts_at_the_same_offset_converge() -> None:
    """The classic case: two people type at the same caret with no knowledge of each other."""
    server = DocumentServer(DOC)
    revision, text = server.snapshot()
    alice = ClientDocument("alice", text, revision)
    bob = ClientDocument("bob", text, revision)

    alice.edit(Insert(6, "big "))
    bob.edit(Insert(6, "cruel "))
    assert (alice.text, bob.text) == ("hello big world", "hello cruel world")

    assert alice.push(server) is True
    assert bob.push(server) is True  # bob's base revision is 0, one behind the server
    assert server.ops_since(1)[0].op == Insert(10, "cruel ")  # rebased past "big "
    assert server.text == "hello big cruel world"

    alice.pull(server)
    bob.pull(server)
    assert alice.text == bob.text == server.text == "hello big cruel world"
    assert alice.revision == bob.revision == server.revision == 2
    assert alice.outstanding is None and bob.outstanding is None


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (Insert(6, "big "), Insert(6, "cruel ")),  # same offset, tie-break decides the order
        (Insert(6, "big "), Insert(0, "oh ")),  # before
        (Insert(6, "big "), Insert(11, "!")),  # after
        (Insert(6, "X"), Delete(6, 5)),  # insert at the start of a deleted range
        (Insert(11, "X"), Delete(6, 5)),  # insert at the end of a deleted range
        (Insert(8, "X"), Delete(6, 5)),  # insert strictly inside a deleted range
        (Delete(0, 5), Delete(6, 5)),  # disjoint deletes
        (Delete(0, 8), Delete(6, 5)),  # partially overlapping deletes
        (Delete(6, 5), Delete(0, 11)),  # one delete fully covers the other
        (Delete(2, 3), Insert(2, "X")),  # insert at the start of the delete
        (Delete(2, 3), Insert(5, "X")),  # insert at the end of the delete
        (Delete(2, 3), Insert(3, "X")),  # insert inside the delete
    ],
)
def test_transform_converges_for_every_op_pair(first: Op, second: Op) -> None:
    left, right = both_orders(DOC, first, second)
    assert left == right


def test_randomised_op_pairs_converge() -> None:
    rng = random.Random(42)
    doc = "abcdefghijkl"

    def make() -> Op:
        pos = rng.randrange(len(doc))
        if rng.random() < 0.5:
            return Insert(pos, rng.choice(["X", "YZ", "123"]))
        return Delete(pos, rng.randrange(1, len(doc) - pos + 1))

    for _ in range(400):
        left, right = both_orders(doc, make(), make())
        assert left == right


def test_insert_inside_a_deleted_range_is_swallowed() -> None:
    """The documented cost of a single-op transform: the delete wins and the typed text is lost."""
    assert transform(Insert(8, "X"), Delete(6, 5)) is None
    assert transform(Delete(6, 5), Insert(8, "X"), op_first=True) == Delete(6, 6)
    server = DocumentServer("hello big cruel world")
    revision, text = server.snapshot()
    carol = ClientDocument("carol", text, revision)
    dave = ClientDocument("dave", text, revision)
    carol.edit(Delete(6, 10))
    dave.edit(Insert(10, "very "))
    carol.push(server)
    dave.push(server)
    carol.pull(server)
    dave.pull(server)
    assert server.text == carol.text == dave.text == "hello world"
    assert server.ops_since(1)[0].op is None  # dave's op consumed a revision but did nothing


def test_overlapping_deletes_do_not_delete_twice() -> None:
    assert transform(Delete(1, 3), Delete(2, 2)) == Delete(1, 1)  # only the surviving part
    assert transform(Delete(2, 2), Delete(1, 3)) is None  # fully covered
    assert transform(Delete(6, 5), Delete(0, 5)) == Delete(1, 5)  # disjoint, shifted left
    assert transform(Delete(0, 5), Delete(6, 5)) == Delete(0, 5)  # disjoint, unchanged
    # "hello world" minus "ll" is "heo world"; the rebased delete then removes only the "e" left
    # of it, which is exactly what Delete(1, 3) alone would have produced.
    assert apply_op(apply_op(DOC, Delete(2, 2)), Delete(1, 1)) == apply_op(DOC, Delete(1, 3))


def test_server_rebases_a_stale_client_and_numbers_revisions() -> None:
    server = DocumentServer(DOC)
    assert server.submit("a", Insert(0, "1"), 0).revision == 1
    assert server.submit("b", Insert(0, "2"), 1).revision == 2
    stale = server.submit("c", Insert(0, "3"), 0)  # two revisions behind
    assert stale.revision == 3
    assert stale.op == Insert(2, "3")  # rebased past both earlier inserts
    assert server.text == "213" + DOC
    assert [entry.client_id for entry in server.ops_since(0)] == ["a", "b", "c"]
    assert server.ops_since(3) == []
    assert server.snapshot() == (3, "213" + DOC)


def test_validation_errors() -> None:
    server = DocumentServer(DOC)
    with pytest.raises(ValidationError):
        Insert(-1, "x")
    with pytest.raises(ValidationError):
        Insert(0, "")
    with pytest.raises(ValidationError):
        Delete(0, 0)
    with pytest.raises(ValidationError):
        apply_op(DOC, Insert(99, "x"))
    with pytest.raises(ValidationError):
        apply_op(DOC, Delete(9, 99))
    with pytest.raises(ValidationError):
        server.submit("a", Insert(0, "x"), base_revision=7)
    with pytest.raises(ValidationError):
        server.ops_since(-1)


def test_a_client_allows_only_one_op_in_flight() -> None:
    server = DocumentServer(DOC)
    client = ClientDocument("solo", DOC, 0)
    client.edit(Insert(0, "a"))
    with pytest.raises(InvalidStateError):
        client.edit(Insert(0, "b"))
    assert client.push(server) is True
    assert client.push(server) is False  # already in flight: never submit twice
    client.pull(server)
    assert client.outstanding is None
    client.edit(Insert(0, "b"))  # allowed again once the ack has been pulled
    client.push(server)
    client.pull(server)
    assert client.text == server.text == "ba" + DOC


def test_concurrent_submits_get_unique_contiguous_revisions() -> None:
    server = DocumentServer("")
    with ThreadPoolExecutor(max_workers=8) as pool:
        applied = list(pool.map(lambda i: server.submit(f"c{i % 8}", Insert(0, "x"), 0), range(300)))
    assert sorted(entry.revision for entry in applied) == list(range(1, 301))
    assert server.text == "x" * 300  # every op survived the rebase; none was applied twice
    assert server.revision == 300
    joiner = ClientDocument("late", *reversed(server.snapshot()))
    assert joiner.text == server.text and joiner.revision == 300
