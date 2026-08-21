from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, NotFoundError, ValidationError
from hld.two_phase_commit import (
    Coordinator,
    CrashPoint,
    Decision,
    NodeDownError,
    Participant,
    Vote,
)


def make_cluster() -> tuple[Coordinator, Participant, Participant, Participant]:
    orders = Participant("orders", {"alice": 100})
    payments = Participant("payments", {"bob": 100})
    inventory = Participant("inventory", {"sku-1": 1})
    coordinator = Coordinator()
    for participant in (orders, payments, inventory):
        coordinator.register(participant)
    return coordinator, orders, payments, inventory


def test_unanimous_yes_commits_everywhere_and_releases_locks() -> None:
    coordinator, orders, payments, _ = make_cluster()
    decision = coordinator.run("tx-1", {"orders": {"alice": -30}, "payments": {"bob": 30}})
    assert decision is Decision.COMMIT
    assert (orders.get("alice"), payments.get("bob")) == (70, 130)
    assert orders.locked_keys() == payments.locked_keys() == {}
    assert orders.in_doubt() == payments.in_doubt() == []
    assert coordinator.pending() == []


def test_a_single_no_vote_aborts_every_participant() -> None:
    coordinator, orders, _, inventory = make_cluster()
    decision = coordinator.run("tx-2", {"orders": {"alice": -10}, "inventory": {"sku-1": -2}})
    assert decision is Decision.ABORT
    assert orders.get("alice") == 100  # the YES voter rolled back
    assert inventory.get("sku-1") == 1
    assert orders.locked_keys() == {}
    assert coordinator.decision_of("tx-2") is Decision.ABORT


def test_participant_down_in_phase_one_counts_as_a_no_vote() -> None:
    coordinator, orders, payments, _ = make_cluster()
    payments.crash()
    decision = coordinator.run("tx-3", {"orders": {"alice": -10}, "payments": {"bob": 10}})
    assert decision is Decision.ABORT
    assert orders.get("alice") == 100
    assert coordinator.pending() == ["tx-3"]  # payments has not heard the abort yet
    payments.recover(coordinator)
    assert payments.in_doubt() == []  # it never voted, so it has nothing to ask about
    assert coordinator.recover() == ["tx-3"]
    assert coordinator.pending() == []


def test_coordinator_crash_before_the_decision_blocks_yes_voters() -> None:
    coordinator, orders, payments, _ = make_cluster()
    coordinator.crash_at(CrashPoint.BEFORE_DECISION_LOGGED)
    with pytest.raises(NodeDownError):
        coordinator.run("tx-4", {"orders": {"alice": -10}, "payments": {"bob": 10}})
    # both voted YES and are now in doubt, holding their locks
    assert orders.in_doubt() == payments.in_doubt() == ["tx-4"]
    assert orders.locked_keys() == {"alice": "tx-4"}
    assert orders.prepare("tx-5", {"alice": -1}) is Vote.NO  # blocked by tx-4's lock
    # a participant that asks while the coordinator is down stays in doubt
    payments.recover(coordinator)
    assert payments.in_doubt() == ["tx-4"]
    # presumed abort once the coordinator is back
    assert coordinator.recover() == ["tx-4"]
    assert coordinator.decision_of("tx-4") is Decision.ABORT
    assert orders.in_doubt() == payments.in_doubt() == []
    assert orders.locked_keys() == {}
    assert (orders.get("alice"), payments.get("bob")) == (100, 100)
    assert orders.prepare("tx-5", {"alice": -1}) is Vote.YES


def test_coordinator_crash_after_logging_commit_is_replayed_on_recovery() -> None:
    coordinator, orders, payments, _ = make_cluster()
    coordinator.crash_at(CrashPoint.AFTER_DECISION_LOGGED)
    with pytest.raises(NodeDownError):
        coordinator.run("tx-6", {"orders": {"alice": -20}, "payments": {"bob": 20}})
    assert coordinator.decision_of("tx-6") is Decision.COMMIT  # durable, nobody told yet
    assert orders.get("alice") == 100
    assert coordinator.recover() == ["tx-6"]
    assert (orders.get("alice"), payments.get("bob")) == (80, 120)
    assert coordinator.pending() == []


def test_participant_that_dies_after_voting_yes_asks_the_coordinator_on_recovery() -> None:
    coordinator, orders, payments, _ = make_cluster()
    payments.crash_after_vote()
    decision = coordinator.run("tx-7", {"orders": {"alice": -5}, "payments": {"bob": 5}})
    assert decision is Decision.COMMIT
    assert orders.get("alice") == 95
    assert payments.in_doubt() == ["tx-7"]
    assert payments.get("bob") == 100  # prepared, not yet applied
    assert coordinator.pending() == ["tx-7"]
    payments.recover(coordinator)
    assert payments.get("bob") == 105
    assert payments.in_doubt() == []


def test_phase_two_messages_are_idempotent_and_unknown_transactions_presume_abort() -> None:
    coordinator, orders, payments, _ = make_cluster()
    coordinator.run("tx-8", {"orders": {"alice": -1}, "payments": {"bob": 1}})
    orders.commit("tx-8")  # a duplicate commit changes nothing
    orders.abort("never-prepared")  # a stray abort is harmless
    assert orders.get("alice") == 99
    assert orders.prepare("tx-9", {"alice": -1}) is Vote.YES
    assert orders.prepare("tx-9", {"alice": -1}) is Vote.YES  # retried prepare: same answer
    assert coordinator.decision_for("tx-unknown") is Decision.ABORT


def test_validation_and_registration_errors() -> None:
    coordinator, *_ = make_cluster()
    with pytest.raises(ValidationError):
        coordinator.run("tx-empty", {})
    with pytest.raises(NotFoundError):
        coordinator.run("tx-x", {"shipping": {"parcel": 1}})
    coordinator.run("tx-dup", {"orders": {"alice": -1}})
    with pytest.raises(ConflictError):
        coordinator.run("tx-dup", {"orders": {"alice": -1}})
    with pytest.raises(ValidationError):
        Participant("")


def test_concurrent_transfers_conserve_money_and_release_every_lock() -> None:
    accounts = {f"acct-{i}": 100 for i in range(8)}
    bank_a = Participant("bank_a", accounts)
    bank_b = Participant("bank_b", accounts)
    coordinator = Coordinator()
    coordinator.register(bank_a)
    coordinator.register(bank_b)

    def transfer(i: int) -> Decision:
        src, dst = f"acct-{i % 8}", f"acct-{(i * 3 + 1) % 8}"
        return coordinator.run(f"tx-{i}", {"bank_a": {src: -7}, "bank_b": {dst: 7}})

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(transfer, range(200)))

    total = sum(bank_a.get(k) for k in accounts) + sum(bank_b.get(k) for k in accounts)
    assert total == 2 * 8 * 100  # every commit moved 7 from one bank to the other
    assert all(bank_a.get(k) >= 0 for k in accounts)
    assert bank_a.locked_keys() == bank_b.locked_keys() == {}
    assert bank_a.in_doubt() == bank_b.in_doubt() == []
    assert coordinator.pending() == []
    committed = decisions.count(Decision.COMMIT)
    assert committed >= 1
    assert sum(bank_b.get(k) for k in accounts) == 8 * 100 + 7 * committed
