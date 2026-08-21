from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, Money, SequentialIdGenerator
from lld.splitwise.commands import AddExpenseCommand, CommandHistory, DeleteExpenseCommand
from lld.splitwise.models import (
    ExpenseStateError,
    ExpenseStatus,
    Group,
    GroupMembershipError,
    SplitType,
    UnbalancedExpenseError,
    User,
)
from lld.splitwise.services import ActivityFeed, DebtSimplifier, ExpenseService, SplitwiseStore

MEMBERS = ("alice", "bob", "carol", "dave")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000)


def make_service(clock: FakeClock, members: tuple[str, ...] = MEMBERS) -> ExpenseService:
    store = SplitwiseStore()
    for name in members:
        store.add_user(User(name, name.title(), f"{name}@example.com"))
    store.create_group(Group("g1", "Trip", set(members)))
    return ExpenseService(store, clock=clock, ids=SequentialIdGenerator("X"), listeners=[ActivityFeed()])


def test_equal_split_of_an_odd_amount_keeps_every_cent(clock: FakeClock) -> None:
    service = make_service(clock, ("alice", "bob", "carol"))
    expense = service.add_expense("g1", "Dinner", {"alice": Money.of("100.01")}, ("alice", "bob", "carol"))
    assert [s.amount.cents for s in expense.owed_by] == [3334, 3334, 3333]
    assert service.balance_between("g1", "bob", "alice") == Money.of("33.34")
    assert sum(m.cents for m in service.balances("g1").values()) == 0


@pytest.mark.parametrize(
    ("split_type", "weights", "expected"),
    [
        (SplitType.EQUAL, None, [2500, 2500, 2500, 2500]),
        (SplitType.EXACT, (4000, 3000, 2000, 1000), [4000, 3000, 2000, 1000]),
        (SplitType.PERCENT, (5000, 2500, 1250, 1250), [5000, 2500, 1250, 1250]),
        (SplitType.SHARE, (2, 1, 1, 1), [4000, 2000, 2000, 2000]),
    ],
)
def test_every_split_type_adds_up_to_the_total(
    clock: FakeClock, split_type: SplitType, weights: tuple[int, ...] | None, expected: list[int]
) -> None:
    service = make_service(clock)
    expense = service.add_expense(
        "g1", "Rent", {"alice": Money.of("100.00")}, MEMBERS, split_type=split_type, weights=weights
    )
    assert [s.amount.cents for s in expense.owed_by] == expected
    assert sum(s.amount.cents for s in expense.owed_by) == 10_000


def test_shares_that_do_not_add_up_are_rejected_and_nothing_is_written(clock: FakeClock) -> None:
    service = make_service(clock)
    with pytest.raises(UnbalancedExpenseError):
        service.add_expense(
            "g1", "Rent", {"alice": Money.of("100.00")}, MEMBERS,
            split_type=SplitType.EXACT, weights=(4000, 3000, 2000, 500),
        )
    with pytest.raises(GroupMembershipError):
        service.add_expense("g1", "Rent", {"alice": Money.of("10.00")}, ("alice", "eve"))
    assert service.balances("g1") == {name: Money(0) for name in MEMBERS}
    assert service.ledger("g1") == []


def test_editing_an_expense_recalculates_balances_and_supersedes_the_old_version(clock: FakeClock) -> None:
    service = make_service(clock, ("alice", "bob"))
    original = service.add_expense("g1", "Hotel", {"alice": Money.of("300.00")}, ("alice", "bob"))
    assert service.balance_between("g1", "bob", "alice") == Money.of("150.00")
    edited = service.edit_expense("g1", original.id, "Hotel", {"alice": Money.of("280.00")}, ("alice", "bob"))
    assert service.balance_between("g1", "bob", "alice") == Money.of("140.00")
    assert service.expense("g1", original.id).status is ExpenseStatus.SUPERSEDED
    assert edited.replaces_id == original.id and edited.status is ExpenseStatus.ACTIVE
    with pytest.raises(ExpenseStateError):
        service.delete_expense("g1", original.id, "alice")


# --8<-- [start:undo]
def test_delete_reverses_the_balances_and_undo_puts_them_back(clock: FakeClock) -> None:
    service = make_service(clock, ("alice", "bob"))
    history = CommandHistory()
    history.run(AddExpenseCommand(service, "g1", "Taxi", {"alice": Money.of("40.00")}, ("alice", "bob")))
    expense_id = service.ledger("g1")[0].expense_id
    assert service.balance_between("g1", "bob", "alice") == Money.of("20.00")

    history.run(DeleteExpenseCommand(service, "g1", expense_id, "bob"))
    assert service.balance_between("g1", "bob", "alice") == Money(0)
    assert service.expense("g1", expense_id).status is ExpenseStatus.DELETED

    history.undo_last()  # restore the delete
    assert service.balance_between("g1", "bob", "alice") == Money.of("20.00")
    history.undo_last()  # undo the original add
    assert service.balance_between("g1", "bob", "alice") == Money(0)


# --8<-- [end:undo]


# --8<-- [start:concurrency]
def test_concurrent_expenses_keep_the_group_balanced(clock: FakeClock) -> None:
    service = make_service(clock)

    def add(i: int) -> None:
        payer = MEMBERS[i % len(MEMBERS)]
        service.add_expense("g1", f"Round {i}", {payer: Money.of("10.00")}, MEMBERS, actor_id=payer)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(40)))

    balances = service.balances("g1")
    # 40 expenses of 10.00, each split four ways, ten paid by each member: everyone is square.
    assert sum(m.cents for m in balances.values()) == 0
    assert balances == {name: Money(0) for name in MEMBERS}
    assert len(service.ledger("g1")) == 40 * 3  # three transfers per single-payer expense


# --8<-- [end:concurrency]


def test_simplify_needs_at_most_n_minus_1_transfers(clock: FakeClock) -> None:
    service = make_service(clock)
    service.add_expense("g1", "Villa", {"alice": Money.of("400.00")}, MEMBERS)
    service.add_expense("g1", "Boat", {"bob": Money.of("200.00")}, ("carol", "dave"))
    plan = service.simplify("g1")
    assert len(plan) <= len(MEMBERS) - 1
    settled = dict(service.balances("g1"))
    for transfer in plan:
        service.settle_up("g1", transfer.debtor_id, transfer.creditor_id, transfer.amount)
    assert all(m.is_zero() for m in service.balances("g1").values())
    assert sum(m.cents for m in settled.values()) == 0


def test_simplifier_is_deterministic_and_rejects_a_corrupt_ledger() -> None:
    nets = {"a": Money.of("-30.00"), "b": Money.of("-20.00"), "c": Money.of("50.00")}
    plan = DebtSimplifier().simplify(nets)
    assert [(t.debtor_id, t.creditor_id, t.amount.cents) for t in plan] == [("a", "c", 3000), ("b", "c", 2000)]
    with pytest.raises(Exception, match="sum to zero"):
        DebtSimplifier().simplify({"a": Money.of("-30.00"), "b": Money.of("10.00")})


def test_global_balance_sums_every_group(clock: FakeClock) -> None:
    store = SplitwiseStore()
    for name in ("alice", "bob"):
        store.add_user(User(name, name.title(), f"{name}@example.com"))
    store.create_group(Group("g1", "Trip", {"alice", "bob"}))
    store.create_group(Group("g2", "Flat", {"alice", "bob"}))
    service = ExpenseService(store, clock=clock, ids=SequentialIdGenerator("X"))
    service.add_expense("g1", "Hotel", {"alice": Money.of("100.00")}, ("alice", "bob"))
    service.add_expense("g2", "Rent", {"bob": Money.of("30.00")}, ("alice", "bob"))
    assert service.global_balance("alice") == Money.of("35.00")
    assert service.global_balance("bob") == Money.of("-35.00")
