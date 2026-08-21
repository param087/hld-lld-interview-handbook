from concurrent.futures import ThreadPoolExecutor

import pytest

from common import (
    ConflictError,
    FakeClock,
    InvalidStateError,
    Money,
    NotFoundError,
    ValidationError,
)
from hld.event_sourcing import (
    AccountClosed,
    AccountOpened,
    AccountRepository,
    AccountSummaryProjection,
    BankAccount,
    EventStore,
    LargeMovementsProjection,
    MoneyDeposited,
    MoneyWithdrawn,
    describe,
)


def usd(amount: str) -> Money:
    return Money.of(amount)


def make_repo(snapshot_every: int = 100) -> tuple[EventStore, AccountRepository]:
    store = EventStore(FakeClock(start=1_000.0))
    return store, AccountRepository(store, snapshot_every=snapshot_every)


def test_commands_raise_events_and_apply_state() -> None:
    account = BankAccount.open("acc-1", "ann")
    account.deposit(usd("100.00"))
    account.withdraw(usd("30.00"))
    assert account.balance == usd("70.00")
    assert account.version == 3
    assert account.pending_events == (
        AccountOpened("ann"),
        MoneyDeposited(usd("100.00")),
        MoneyWithdrawn(usd("30.00")),
    )
    assert [describe(e) for e in account.pending_events] == [
        "Opened(ann)",
        "Deposited(100.00)",
        "Withdrawn(30.00)",
    ]
    account.withdraw(usd("70.00"))
    account.close("done")
    assert account.closed and account.version == 5
    assert account.pending_events[-1] == AccountClosed("done")


@pytest.mark.parametrize(
    ("command", "error"),
    [
        (lambda a: a.deposit(usd("0.00")), ValidationError),
        (lambda a: a.deposit(usd("-5.00")), ValidationError),
        (lambda a: a.withdraw(usd("10.01")), InvalidStateError),  # balance is 10.00
        (lambda a: a.close("rich"), InvalidStateError),  # close needs a zero balance
    ],
)
def test_invariants_reject_bad_commands_without_raising_events(command, error) -> None:
    account = BankAccount.open("acc-1", "ann")
    account.deposit(usd("10.00"))
    with pytest.raises(error):
        command(account)
    assert account.version == 2 and len(account.pending_events) == 2


def test_commands_on_missing_or_closed_accounts_fail() -> None:
    with pytest.raises(ValidationError):
        BankAccount.open("", "ann")
    ghost = BankAccount("acc-9")
    with pytest.raises(InvalidStateError):
        ghost.deposit(usd("1.00"))
    account = BankAccount.open("acc-1", "ann")
    account.close("unused")
    with pytest.raises(InvalidStateError):
        account.deposit(usd("1.00"))


def test_replay_rebuilds_the_same_state_with_and_without_a_snapshot() -> None:
    store, repo = make_repo()
    account = BankAccount.open("acc-1", "ann")
    account.deposit(usd("100.00"))
    account.withdraw(usd("30.00"))
    repo.save(account)
    history = [e.event for e in store.load("acc-1")]
    rebuilt = BankAccount.replay("acc-1", history)
    assert rebuilt == account
    assert rebuilt.pending_events == ()
    # snapshot at version 2 plus the events after it gives the same aggregate again
    at_two = BankAccount.replay("acc-1", history[:2]).snapshot()
    assert at_two.version == 2 and at_two.balance == usd("100.00")
    assert BankAccount.replay("acc-1", history[2:], snapshot=at_two) == account
    with pytest.raises(ValidationError):
        BankAccount.replay("acc-2", history[2:], snapshot=at_two)


def test_optimistic_concurrency_rejects_the_stale_writer() -> None:
    store, repo = make_repo()
    repo.save(BankAccount.open("acc-1", "ann"))
    first, second = repo.load("acc-1"), repo.load("acc-1")
    first.deposit(usd("1.00"))
    second.deposit(usd("2.00"))
    assert [e.version for e in repo.save(first)] == [2]
    with pytest.raises(ConflictError):
        repo.save(second)
    assert store.version("acc-1") == 2
    assert repo.load("acc-1").balance == usd("1.00")
    # the loser reloads and retries: its command is re-validated against the fresh state
    retry = repo.load("acc-1")
    retry.deposit(usd("2.00"))
    repo.save(retry)
    assert repo.load("acc-1").balance == usd("3.00")


def test_store_positions_versions_and_validation() -> None:
    store = EventStore(FakeClock(start=42.0))
    a = store.append("a", [AccountOpened("ann"), MoneyDeposited(usd("1.00"))], expected_version=0)
    b = store.append("b", [AccountOpened("bob")], expected_version=0)
    assert [(e.stream_id, e.version, e.position) for e in a + b] == [("a", 1, 1), ("a", 2, 2), ("b", 1, 3)]
    assert all(e.timestamp == 42.0 for e in a + b)
    assert [e.position for e in store.read_all(after_position=1)] == [2, 3]
    assert store.load("a", after_version=1) == a[1:]
    assert store.load("missing") == [] and store.version("missing") == 0
    with pytest.raises(ValidationError):
        store.append("a", [], expected_version=2)
    with pytest.raises(ValidationError):
        store.append("a", [AccountClosed("x")], expected_version=-1)
    with pytest.raises(ConflictError):
        store.append("a", [AccountClosed("x")], expected_version=1)
    with pytest.raises(NotFoundError):
        AccountRepository(store).load("missing")
    with pytest.raises(ValidationError):
        AccountRepository(store, snapshot_every=0)


def test_snapshots_shorten_the_load_path() -> None:
    store, repo = make_repo(snapshot_every=10)
    repo.save(BankAccount.open("acc-1", "ann"))
    for _ in range(24):
        account = repo.load("acc-1")
        account.deposit(usd("1.00"))
        repo.save(account)
    snapshot = store.snapshot("acc-1")
    assert snapshot is not None and snapshot.version == 20
    assert snapshot.balance == usd("19.00")
    assert len(store.load("acc-1", after_version=snapshot.version)) == 5
    loaded = repo.load("acc-1")
    assert loaded.version == 25 and loaded.balance == usd("24.00")
    # a stale snapshot never replaces a newer one
    store.save_snapshot(BankAccount.replay("acc-1", [AccountOpened("ann")]).snapshot())
    assert store.snapshot("acc-1") == snapshot


def test_projection_is_incremental_and_idempotent() -> None:
    store, repo = make_repo()
    ann = BankAccount.open("acc-1", "ann")
    ann.deposit(usd("100.00"))
    repo.save(ann)
    summary = AccountSummaryProjection()
    assert summary.catch_up(store) == 2
    assert summary.rows["acc-1"].balance == usd("100.00")
    assert summary.checkpoint == 2
    # redelivery of already-applied events is ignored
    assert all(not summary.apply(e) for e in store.read_all())
    assert summary.rows["acc-1"].deposits == 1
    # new events are picked up incrementally
    bob = BankAccount.open("acc-2", "bob")
    bob.deposit(usd("250.00"))
    bob.withdraw(usd("50.00"))
    repo.save(bob)
    assert summary.catch_up(store) == 3
    assert summary.top_balances(2) == [("acc-2", usd("200.00")), ("acc-1", usd("100.00"))]
    assert (summary.rows["acc-2"].deposits, summary.rows["acc-2"].withdrawals) == (1, 1)
    assert summary.catch_up(store) == 0


def test_a_new_projection_is_built_from_the_full_history() -> None:
    store, repo = make_repo()
    ann = BankAccount.open("acc-1", "ann")
    ann.deposit(usd("500.00"))
    ann.withdraw(usd("20.00"))
    ann.withdraw(usd("480.00"))
    ann.close("left")
    repo.save(ann)
    audit = LargeMovementsProjection(threshold=usd("100.00"))
    assert audit.rebuild(store) == 5
    assert audit.movements == [
        ("acc-1", "deposit", usd("500.00")),
        ("acc-1", "withdrawal", usd("480.00")),
    ]
    assert audit.rebuild(store) == 5  # rebuilding resets the model instead of doubling it
    assert len(audit.movements) == 2
    summary = AccountSummaryProjection()
    summary.rebuild(store)
    assert summary.rows["acc-1"].closed and summary.rows["acc-1"].balance.is_zero()


def test_concurrent_writers_retry_on_conflict_and_never_lose_money() -> None:
    store, repo = make_repo(snapshot_every=25)
    repo.save(BankAccount.open("acc-1", "ann"))
    deposits_per_worker = 25

    def worker(i: int) -> int:
        conflicts = 0
        for _ in range(deposits_per_worker):
            while True:
                account = repo.load("acc-1")
                account.deposit(usd("1.00"))
                try:
                    repo.save(account)
                    break
                except ConflictError:
                    conflicts += 1
        return conflicts

    with ThreadPoolExecutor(max_workers=8) as pool:
        conflicts = list(pool.map(worker, range(8)))
    total = 8 * deposits_per_worker
    assert store.version("acc-1") == total + 1
    assert repo.load("acc-1").balance == Money(total * 100)
    assert [e.version for e in store.load("acc-1")] == list(range(1, total + 2))
    assert [e.position for e in store.read_all()] == list(range(1, total + 2))
    assert sum(conflicts) >= 0  # conflicts are possible, lost updates are not
    summary = AccountSummaryProjection()
    summary.catch_up(store)
    assert summary.rows["acc-1"].deposits == total
