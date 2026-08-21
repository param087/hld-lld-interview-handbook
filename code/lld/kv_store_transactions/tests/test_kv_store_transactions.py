import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, SequentialIdGenerator, ValidationError
from lld.kv_store_transactions.models import (
    CommandError,
    KeyMissingError,
    NoTransactionError,
    TransactionConflictError,
    ValueTypeError,
)
from lld.kv_store_transactions.repl import CommandParser
from lld.kv_store_transactions.services import InMemoryLog, KVStore
from lld.kv_store_transactions.transactions import LastWriteWins, OptimisticIsolation


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_700_000_000)


@pytest.fixture
def store(clock: FakeClock) -> KVStore:
    return KVStore(clock=clock, ids=SequentialIdGenerator("tx"))


def test_reads_see_their_own_uncommitted_writes(store: KVStore) -> None:
    store.set("a", "committed")
    store.begin()
    store.set("a", "staged")
    assert store.get("a") == "staged" and store.exists("a")
    store.rollback()
    assert store.get("a") == "committed" and store.depth == 0


# --8<-- [start:nesting]
def test_an_inner_commit_reaches_the_parent_and_no_further(store: KVStore) -> None:
    """The semantic every interviewer checks: a nested COMMIT is not durable.

    `SET a 2` is committed by the inner transaction, so the outer one can see it -
    and the outer ROLLBACK then throws away both levels of work.
    """
    store.begin()
    store.set("a", 1)
    store.begin()
    store.set("a", 2)
    store.commit()  # merges into the outer write-set, not into storage
    assert store.get("a") == 2 and store.depth == 1
    store.rollback()
    assert store.get("a") is None and store.depth == 0


def test_an_inner_rollback_drops_only_its_own_level(store: KVStore) -> None:
    store.set("a", "base")
    store.begin()
    store.set("a", "outer")
    store.begin()
    store.delete("a")
    assert store.get("a") is None  # the tombstone hides the outer write
    store.rollback()
    assert store.get("a") == "outer"  # ... and only the tombstone went away
    store.commit()
    assert store.get("a") == "outer"


# --8<-- [end:nesting]


def test_a_delete_inside_a_transaction_commits_as_a_removal(store: KVStore) -> None:
    store.set("a", 1)
    store.set("b", 2)
    store.begin()
    assert store.delete("a") is True
    assert store.delete("missing") is False
    store.commit()
    assert store.scan() == [("b", 2)] and len(store) == 1


@pytest.mark.parametrize("verb", ["commit", "rollback"])
def test_commit_or_rollback_without_a_transaction_is_an_error(store: KVStore, verb: str) -> None:
    with pytest.raises(NoTransactionError):
        getattr(store, verb)()


def test_incr_composes_with_transactions_and_refuses_text(store: KVStore) -> None:
    assert store.incr("hits") == 1  # a missing key counts as zero
    store.begin()
    assert store.incr("hits", 5) == 6
    assert store.decr("hits", 2) == 4
    store.rollback()
    assert store.get("hits") == 1
    store.set("name", "alice")
    with pytest.raises(ValueTypeError):
        store.incr("name")


def test_ttl_is_absolute_and_survives_a_commit(store: KVStore, clock: FakeClock) -> None:
    store.begin()
    store.set("session", "token", ttl=300.0)  # the deadline is fixed here, not at COMMIT
    clock.advance(200)
    store.commit()
    assert store.get("session") == "token"
    clock.advance(101)
    assert store.get("session") is None and store.exists("session") is False
    with pytest.raises(KeyMissingError):
        _ = store["session"]
    assert store.purge_expired() == 1
    with pytest.raises(ValidationError):
        store.set("x", 1, ttl=0)


def test_scan_merges_the_chain_and_iteration_is_a_snapshot(store: KVStore) -> None:
    store.set("user:1", "alice")
    store.set("order:9", 42)
    store.begin()
    store.set("user:2", "bob")
    store.delete("user:1")
    assert store.scan("user:") == [("user:2", "bob")]
    snapshot = list(store)
    store.set("user:3", "carol")  # writing during iteration cannot disturb the snapshot
    assert snapshot == [("order:9", 42), ("user:2", "bob")]
    assert store.count("bob") == 1


# --8<-- [start:isolation]
def test_two_sessions_never_see_each_other_s_staged_writes(store: KVStore) -> None:
    """The stack is per thread, so a transaction is invisible until it commits."""
    store.set("k", "base")
    gate = threading.Barrier(2)
    seen: dict[str, object] = {}

    def writer() -> None:
        store.begin()
        store.set("k", "staged")
        gate.wait()  # hold the transaction open while the reader looks
        gate.wait()
        store.commit()

    def reader() -> None:
        gate.wait()
        seen["during"] = store.get("k")
        gate.wait()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda job: job(), [writer, reader]))
    assert seen["during"] == "base"  # uncommitted work was never visible
    assert store.get("k") == "staged"


def test_autocommit_incr_is_atomic_under_load(store: KVStore) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.incr("counter"), range(800)))
    assert store.get("counter") == 800  # read-modify-write happens under one lock


# --8<-- [end:isolation]


def test_optimistic_isolation_rejects_a_commit_built_on_a_stale_read(clock: FakeClock) -> None:
    store = KVStore(clock=clock, ids=SequentialIdGenerator("tx"), isolation=OptimisticIsolation())
    store.set("balance", 100)
    store.begin()
    assert store.get("balance") == 100  # the version is noted here

    def interfere() -> None:
        store.set("balance", 90)  # a different session, autocommitting

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(interfere).result()

    store.set("balance", 80)
    with pytest.raises(TransactionConflictError):
        store.commit()
    assert store.get("balance") == 90 and store.depth == 0  # the loser's work is gone


def test_last_write_wins_accepts_the_same_commit(clock: FakeClock) -> None:
    store = KVStore(clock=clock, ids=SequentialIdGenerator("tx"), isolation=LastWriteWins())
    store.set("balance", 100)
    store.begin()
    store.get("balance")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: store.set("balance", 90)).result()
    store.set("balance", 80)
    store.commit()
    assert store.get("balance") == 80  # the interfering write is silently lost


def test_the_log_appends_one_batch_per_commit_and_restore_replays_it(clock: FakeClock) -> None:
    log = InMemoryLog()
    store = KVStore(clock=clock, ids=SequentialIdGenerator("tx"), log=log)
    store.begin()
    store.set("a", 1)
    store.set("b", 2)
    store.set("c", 3)
    store.commit()
    assert log.batches == 1 and len(log.replay()) == 3  # a commit is one atomic append
    store.begin()
    store.set("d", 4)
    store.rollback()
    assert log.batches == 1  # a rollback writes nothing at all
    restored = KVStore.restore(log, clock=clock)
    assert restored.scan() == [("a", 1), ("b", 2), ("c", 3)]


def test_command_parser_runs_a_script(store: KVStore) -> None:
    parser = CommandParser(store)
    replies = parser.run(
        ["SET a 1", "GET a", "GET missing", "EXISTS a", "INCR a 4", "COUNT 5", "SCAN a", "DEL a", "SCAN"]
    )
    assert replies == ["OK", "1", "(nil)", "1", "5", "1", "a=5", "1", "(empty)"]


def test_command_parser_drives_transactions(store: KVStore) -> None:
    parser = CommandParser(store)
    replies = parser.run(["SET a 1", "BEGIN", "SET a 2", "BEGIN", "DEL a", "GET a", "ROLLBACK", "GET a", "COMMIT"])
    assert replies[1] == "OK depth=1" and replies[3] == "OK depth=2"
    assert replies[5] == "(nil)" and replies[7] == "2" and replies[8] == "OK depth=0"
    assert store.get("a") == 2


def test_command_parser_reports_errors_instead_of_raising_mid_script(store: KVStore) -> None:
    parser = CommandParser(store)
    replies = parser.run(["SET a 1", "FLUSH", "GET a"])
    assert replies[0] == "OK" and replies[2] == "1"
    assert replies[1].startswith("ERR")
    with pytest.raises(CommandError):
        parser.execute("GET")
    with pytest.raises(CommandError):
        parser.execute("")
