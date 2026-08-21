"""A REPL session that walks the nesting rules, then TTL, recovery and a commit conflict."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from common import FakeClock, SequentialIdGenerator
from lld.kv_store_transactions.models import TransactionConflictError
from lld.kv_store_transactions.repl import CommandParser
from lld.kv_store_transactions.services import InMemoryLog, KVStore
from lld.kv_store_transactions.transactions import OptimisticIsolation

SESSION = [
    "SET user:1 alice",
    "BEGIN",
    "SET user:1 bob",
    "BEGIN",
    "DEL user:1",
    "GET user:1",
    "ROLLBACK",
    "GET user:1",
    "COMMIT",
    "GET user:1",
]


def contested_run() -> tuple[str, str, object]:
    """Two threads, one key: the slow session reads early and commits late."""
    clock = FakeClock(start=1_700_000_000)
    store = KVStore(clock=clock, ids=SequentialIdGenerator("otx"), isolation=OptimisticIsolation())
    store.set("balance", 100)
    gate = threading.Barrier(2)

    def slow_session() -> str:
        store.begin()
        store.get("balance")  # version noted here
        gate.wait()  # let the other writer go
        gate.wait()  # ... and wait until it has finished
        store.set("balance", 80)
        try:
            store.commit()
        except TransactionConflictError:
            return "slow session rejected"
        return "slow session committed"

    def fast_writer() -> str:
        gate.wait()
        store.set("balance", 90)  # autocommit: the version moves
        gate.wait()
        return "fast writer committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow, fast = pool.submit(slow_session), pool.submit(fast_writer)
        return slow.result(), fast.result(), store.get("balance")


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    log = InMemoryLog()
    store = KVStore(clock=clock, ids=SequentialIdGenerator("tx"), log=log)
    repl = CommandParser(store)

    print("--- nesting: an inner ROLLBACK drops its own level and nothing else ---")
    for line, reply in zip(SESSION, repl.run(SESSION), strict=True):
        print(f"  {line:<16} -> {reply}")

    repl.run(["BEGIN", "SET flag on", "BEGIN", "SET flag off", "COMMIT", "ROLLBACK"])
    print(f"inner COMMIT only reaches the parent: after the outer ROLLBACK, GET flag -> {repl.execute('GET flag')}")
    print("counters: " + " | ".join(repl.run(["SET hits 0", "INCR hits 5", "DECR hits 2", "GET hits"])))
    repl.run(["SET user:2 bob", "SET order:9 42"])
    print(f"prefix scan: {repl.execute('SCAN user:')}")

    repl.execute("SET session:7 token EX 300")
    clock.advance(299)
    live = repl.execute("GET session:7")
    clock.advance(2)
    print(f"TTL: at 299 s -> {live}, at 301 s -> {repl.execute('GET session:7')}, {len(store)} keys visible")

    batches = log.batches
    repl.run(["BEGIN", "SET a 1", "SET b 2", "SET c 3", "COMMIT"])
    print(f"durability: a 3-write transaction appended {log.batches - batches} batch, "
          f"{len(log.replay())} records in the log")
    restored = KVStore.restore(log, clock=clock)
    print(f"recovery: restored user:1={restored.get('user:1')} hits={restored.get('hits')} "
          f"with {len(restored)} visible keys")

    slow, fast, balance = contested_run()
    print(f"optimistic isolation: {slow}, {fast}, balance={balance}")


if __name__ == "__main__":
    main()
