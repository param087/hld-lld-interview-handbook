"""Unit of Work: three writes become visible together or not at all, on both implementations."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import (
    ConflictError,
    FakeClock,
    InvalidStateError,
    Money,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)
from patterns.unit_of_work import (
    Account,
    InMemoryUnitOfWork,
    InsufficientFundsError,
    SqliteUnitOfWork,
    TransferService,
    UnitOfWork,
    connect,
    transaction,
)

OPENING = (Account("alice", Money.of("100.00")), Account("bob", Money.of("20.00")))
EPOCH = 1_700_000_000.0


@pytest.fixture(params=["sqlite", "in-memory"])
def uow(request: pytest.FixtureRequest) -> Iterator[UnitOfWork]:
    """Every test below runs once per implementation: the fake must be as atomic as SQLite."""
    if request.param == "in-memory":
        yield InMemoryUnitOfWork(OPENING)
        return
    conn = connect()
    unit = SqliteUnitOfWork(conn)
    with unit as seed:
        for account in OPENING:
            seed.accounts.save(account)
        seed.commit()
    try:
        yield unit
    finally:
        conn.close()


@pytest.fixture
def service(uow: UnitOfWork) -> TransferService:
    return TransferService(uow, SequentialIdGenerator("txn"), FakeClock(start=EPOCH))


def balances(uow: UnitOfWork) -> tuple[Money, Money]:
    with uow as unit:
        return unit.accounts.get("alice").balance, unit.accounts.get("bob").balance


def test_a_transfer_commits_both_balances_and_the_ledger_entry_together(
    uow: UnitOfWork, service: TransferService
) -> None:
    entry = service.transfer("alice", "bob", Money.of("30.00"))
    assert (entry.id, entry.at) == ("txn-1", EPOCH)
    assert balances(uow) == (Money.of("70.00"), Money.of("50.00"))
    assert service.history("alice") == service.history("bob") == [entry]


def test_a_failure_after_two_writes_rolls_all_three_back(
    uow: UnitOfWork, service: TransferService
) -> None:
    service.transfer("alice", "bob", Money.of("30.00"))
    replay = TransferService(uow, SequentialIdGenerator("txn"), FakeClock(start=EPOCH))  # txn-1 again
    with pytest.raises(ConflictError):
        replay.transfer("alice", "bob", Money.of("10.00"))  # both balances were saved before append
    assert balances(uow) == (Money.of("70.00"), Money.of("50.00"))
    assert [entry.id for entry in service.history("alice")] == ["txn-1"]


def test_rejected_transfers_leave_no_trace(uow: UnitOfWork, service: TransferService) -> None:
    with pytest.raises(InsufficientFundsError):
        service.transfer("bob", "alice", Money.of("500.00"))
    with pytest.raises(NotFoundError):
        service.transfer("alice", "carol", Money.of("1.00"))
    with pytest.raises(ValidationError):
        service.transfer("alice", "bob", Money.of("0.00"))
    with pytest.raises(ValidationError):
        service.transfer("alice", "alice", Money.of("1.00"))
    assert balances(uow) == (Money.of("100.00"), Money.of("20.00"))
    assert service.history("alice") == []


def test_only_commit_publishes_and_the_block_is_the_boundary(uow: UnitOfWork) -> None:
    with uow as unit:
        unit.accounts.save(Account("alice", Money.of("90.00")))
        assert unit.accounts.get("alice").balance == Money.of("90.00")  # visible inside the block
    assert balances(uow)[0] == Money.of("100.00")  # discarded: nobody called commit

    with uow as unit:
        unit.accounts.save(Account("alice", Money.of("90.00")))
        unit.commit()
        unit.accounts.save(Account("alice", Money.of("0.00")))  # written after the commit, never committed
    assert balances(uow)[0] == Money.of("90.00")

    with uow as unit:
        unit.accounts.save(Account("bob", Money.of("0.00")))
        unit.rollback()
        assert unit.accounts.get("bob").balance == Money.of("20.00")  # back to the committed state
        unit.accounts.save(Account("bob", Money.of("21.00")))
        unit.commit()
    assert balances(uow) == (Money.of("90.00"), Money.of("21.00"))


def test_nothing_works_outside_the_block(uow: UnitOfWork) -> None:
    with pytest.raises(InvalidStateError):
        uow.commit()
    with pytest.raises(InvalidStateError):
        uow.rollback()
    assert not hasattr(uow, "accounts") and not hasattr(uow, "ledger")
    with uow as unit:
        assert unit.accounts.get("alice") == OPENING[0]
    assert not hasattr(uow, "accounts")  # the repositories went away with the transaction


def test_contextmanager_variant_commits_on_success_and_rolls_back_on_error() -> None:
    conn = connect()
    with transaction(conn) as tx:
        tx.execute("INSERT INTO accounts (id, cents, currency) VALUES ('alice', 10000, 'USD')")
    with pytest.raises(RuntimeError), transaction(conn) as tx:
        tx.execute("UPDATE accounts SET cents = 0 WHERE id = 'alice'")
        raise RuntimeError("power cut before commit")
    assert conn.execute("SELECT cents FROM accounts WHERE id = 'alice'").fetchone() == (10000,)
    assert not conn.in_transaction


def test_concurrent_transfers_conserve_money_and_every_one_is_in_the_ledger() -> None:
    uow = InMemoryUnitOfWork([Account("alice", Money.of("100.00")), Account("bob", Money.of("100.00"))])
    service = TransferService(uow, SequentialIdGenerator("txn"), FakeClock(start=EPOCH))
    legs = [("alice", "bob")] * 100 + [("bob", "alice")] * 100

    def run(leg: tuple[str, str]) -> str:
        return service.transfer(leg[0], leg[1], Money.of("0.50")).id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(run, legs))
    assert len(set(ids)) == 200
    assert balances(uow) == (Money.of("100.00"), Money.of("100.00"))
    assert len(service.history("alice")) == 200
