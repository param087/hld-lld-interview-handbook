"""Two-phase commit (2PC) with crash injection for the coordinator and the participants.

What the module demonstrates, in the order an interviewer asks about it:

* ``Coordinator.run`` drives the protocol: phase 1 sends ``prepare`` to every participant and
  collects votes; the decision becomes durable when it is written to the coordinator's log;
  phase 2 broadcasts ``commit`` or ``abort``.
* ``Participant`` is a resource manager (a key-value store with per-key locks). Voting YES
  writes a prepare record and gives up the right to abort on its own, so a YES voter keeps its
  locks until the decision arrives.
* ``crash`` / ``recover`` on either side show the failure modes: a participant that dies before
  voting counts as a NO (timeout); a participant that dies after voting YES asks the
  coordinator for the outcome when it comes back; a coordinator that dies before logging the
  decision leaves every YES voter *in doubt*, holding locks: the blocking property of 2PC.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from common import ConflictError, HandbookError, InvalidStateError, NotFoundError, ValidationError


# --8<-- [start:protocol]
class Vote(StrEnum):
    YES = "yes"
    NO = "no"


class Decision(StrEnum):
    COMMIT = "commit"
    ABORT = "abort"


class CrashPoint(StrEnum):
    """Where the coordinator dies during ``run``, for failure injection."""

    BEFORE_DECISION_LOGGED = "before logging the decision"
    AFTER_DECISION_LOGGED = "after logging the decision"


class NodeDownError(HandbookError):
    """The remote side is crashed: the caller sees a timeout instead of an answer."""


@dataclass(slots=True)
class TxRecord:
    """One entry of the coordinator's durable log."""

    txid: str
    participants: tuple[str, ...]
    decision: Decision | None = None  # None: prepares were sent, no decision logged yet
    acked: set[str] = field(default_factory=set)

    @property
    def delivered(self) -> bool:
        return self.decision is not None and set(self.participants) <= self.acked


# --8<-- [end:protocol]


# --8<-- [start:participant]
class Participant:
    """A resource manager: a store of integer balances with per-key locks.

    ``_lock`` guards ``_data``, ``_locks``, ``_prepared`` and ``_decided``. ``_prepared`` plays
    the role of the prepare records in the participant's WAL: it survives ``crash()``, which
    is exactly why a recovered participant can still be in doubt.
    """

    def __init__(self, name: str, data: Mapping[str, int] | None = None, floor: int = 0) -> None:
        if not name:
            raise ValidationError("participant name must be non-empty")
        self.name = name
        self._floor = floor  # no value may drop below this (stock >= 0, balance >= 0)
        self._data: dict[str, int] = dict(data or {})
        self._locks: dict[str, str] = {}  # key -> txid holding the lock
        self._prepared: dict[str, dict[str, int]] = {}  # txid -> values to apply on commit
        self._decided: dict[str, Decision] = {}
        self._down = False
        self._crash_after_vote = False
        self._lock = threading.Lock()

    def get(self, key: str) -> int:
        with self._lock:
            return self._data.get(key, 0)

    def in_doubt(self) -> list[str]:
        """Transactions this participant voted YES on and has not heard a decision for."""
        with self._lock:
            return sorted(self._prepared)

    def locked_keys(self) -> dict[str, str]:
        with self._lock:
            return dict(self._locks)

    def crash(self) -> None:
        self._down = True

    def crash_after_vote(self) -> None:
        """Arm a crash for the next ``prepare``: the vote gets out, then the node dies."""
        self._crash_after_vote = True

    def recover(self, coordinator: Coordinator) -> None:
        """Come back and resolve every in-doubt transaction by asking the coordinator."""
        self._down = False
        for txid in self.in_doubt():
            try:
                decision = coordinator.decision_for(txid)
            except NodeDownError:
                return  # coordinator still down: stay in doubt, keep the locks
            with self._lock:
                self._apply_locked(txid, decision)

    def prepare(self, txid: str, deltas: Mapping[str, int]) -> Vote:
        """Phase 1: lock the keys, validate, write the prepare record, vote.

        A YES vote is a promise: the participant can no longer abort on its own, so it keeps
        the locks until ``commit`` or ``abort`` arrives. A NO vote is a unilateral abort.
        """
        with self._lock:
            self._check_up()
            if txid in self._prepared:
                return Vote.YES  # the coordinator retried; the first answer stands
            if any(self._locks.get(key, txid) != txid for key in deltas):
                return Vote.NO  # another transaction holds a key: vote NO instead of waiting
            new_values = {key: self._data.get(key, 0) + delta for key, delta in deltas.items()}
            if any(value < self._floor for value in new_values.values()):
                return Vote.NO  # the write would break the local invariant
            for key in deltas:
                self._locks[key] = txid
            self._prepared[txid] = new_values
            if self._crash_after_vote:
                self._crash_after_vote = False
                self._down = True
            return Vote.YES

    def commit(self, txid: str) -> None:
        """Phase 2: apply the prepared values and release the locks (idempotent)."""
        with self._lock:
            self._check_up()
            if txid not in self._prepared and self._decided.get(txid) is not Decision.COMMIT:
                raise InvalidStateError(f"{self.name}: {txid} is not prepared")
            self._apply_locked(txid, Decision.COMMIT)

    def abort(self, txid: str) -> None:
        """Phase 2: drop the prepared values and release the locks (idempotent)."""
        with self._lock:
            self._check_up()
            self._apply_locked(txid, Decision.ABORT)

    def _apply_locked(self, txid: str, decision: Decision) -> None:
        pending = self._prepared.pop(txid, None)
        if pending is not None and decision is Decision.COMMIT:
            self._data.update(pending)
        for key in [key for key, holder in self._locks.items() if holder == txid]:
            del self._locks[key]
        self._decided[txid] = decision

    def _check_up(self) -> None:
        if self._down:
            raise NodeDownError(f"{self.name} is down")


# --8<-- [end:participant]


# --8<-- [start:coordinator]
class Coordinator:
    """The transaction manager. ``_log`` is its durable log and survives ``crash()``.

    ``_lock`` guards ``_log``, ``_participants`` and the crash flags, so transactions on
    disjoint keys can run concurrently from several threads.
    """

    def __init__(self) -> None:
        self._participants: dict[str, Participant] = {}
        self._log: dict[str, TxRecord] = {}
        self._down = False
        self._crash_at: CrashPoint | None = None
        self._lock = threading.Lock()

    def register(self, participant: Participant) -> None:
        with self._lock:
            self._participants[participant.name] = participant

    def crash_at(self, point: CrashPoint | None) -> None:
        """Arm a crash for the next ``run`` (``None`` disarms)."""
        with self._lock:
            self._crash_at = point

    def crash(self) -> None:
        with self._lock:
            self._down = True

    def decision_of(self, txid: str) -> Decision | None:
        """Peek at the log (for demos and tests): ``None`` while undecided."""
        with self._lock:
            record = self._log.get(txid)
            return record.decision if record else None

    def pending(self) -> list[str]:
        """Decided transactions whose outcome has not reached every participant yet."""
        with self._lock:
            return sorted(txid for txid, record in self._log.items() if not record.delivered)

    def decision_for(self, txid: str) -> Decision:
        """Answer a recovering participant.

        Presumed abort: a transaction the log knows nothing about was never committed. A
        transaction still in its voting phase may be aborted unilaterally, so that is what an
        inquiry does to it.
        """
        with self._lock:
            self._check_up()
            record = self._log.get(txid)
            if record is None:
                return Decision.ABORT
            if record.decision is None:
                record.decision = Decision.ABORT
            return record.decision

    def run(self, txid: str, plan: Mapping[str, Mapping[str, int]]) -> Decision:
        """Run 2PC for ``plan`` (participant name -> key deltas) and return the decision."""
        if not plan:
            raise ValidationError("a transaction needs at least one participant")
        with self._lock:
            self._check_up()
            if txid in self._log:
                raise ConflictError(f"{txid} already exists")
            unknown = sorted(set(plan) - set(self._participants))
            if unknown:
                raise NotFoundError(f"unknown participants: {unknown}")
            record = TxRecord(txid, tuple(plan))
            self._log[txid] = record
        votes = self._collect_votes(txid, plan)
        decision = Decision.COMMIT if all(v is Vote.YES for v in votes.values()) else Decision.ABORT
        self._maybe_crash(CrashPoint.BEFORE_DECISION_LOGGED, txid)
        with self._lock:
            if record.decision is None:
                record.decision = decision  # the commit point: durable before anyone is told
            decision = record.decision
        self._maybe_crash(CrashPoint.AFTER_DECISION_LOGGED, txid)
        self._broadcast(record)
        return decision

    def recover(self) -> list[str]:
        """Replay the log: undecided transactions abort (presumed abort), decided ones are resent."""
        with self._lock:
            self._down = False
            records = list(self._log.values())
        resolved: list[str] = []
        for record in records:
            with self._lock:
                if record.decision is None:
                    record.decision = Decision.ABORT
                delivered = record.delivered
            if not delivered:
                self._broadcast(record)
                resolved.append(record.txid)
        return resolved

    def _collect_votes(self, txid: str, plan: Mapping[str, Mapping[str, int]]) -> dict[str, Vote]:
        votes: dict[str, Vote] = {}
        for name, deltas in plan.items():
            try:
                votes[name] = self._participants[name].prepare(txid, deltas)
            except NodeDownError:
                votes[name] = Vote.NO  # no vote within the timeout counts as NO
        return votes

    def _maybe_crash(self, point: CrashPoint, txid: str) -> None:
        with self._lock:
            if self._crash_at is not point:
                return
            self._crash_at = None
            self._down = True
        raise NodeDownError(f"coordinator crashed {point.value} of {txid}")

    def _broadcast(self, record: TxRecord) -> None:
        """Phase 2: tell every participant; the ones that are down are retried on recovery."""
        if record.decision is None:
            raise InvalidStateError(f"{record.txid} has no decision to broadcast")
        for name in record.participants:
            if name in record.acked:
                continue
            participant = self._participants[name]
            try:
                if record.decision is Decision.COMMIT:
                    participant.commit(record.txid)
                else:
                    participant.abort(record.txid)
            except NodeDownError:
                continue
            with self._lock:
                record.acked.add(name)

    def _check_up(self) -> None:
        if self._down:
            raise NodeDownError("coordinator is down")


# --8<-- [end:coordinator]


def main() -> None:
    orders = Participant("orders", {"alice": 100})
    payments = Participant("payments", {"bob": 100})
    inventory = Participant("inventory", {"sku-1": 1})
    coordinator = Coordinator()
    for participant in (orders, payments, inventory):
        coordinator.register(participant)

    def balances() -> str:
        return f"alice={orders.get('alice')} bob={payments.get('bob')}"

    decision = coordinator.run("tx-1", {"orders": {"alice": -30}, "payments": {"bob": 30}})
    print(f"tx-1 every participant votes YES    -> {decision.value}; {balances()}")

    decision = coordinator.run("tx-2", {"orders": {"alice": -10}, "inventory": {"sku-1": -2}})
    print(
        f"tx-2 inventory votes NO (1 - 2 < 0)  -> {decision.value}; "
        f"orders rolled back, locks={orders.locked_keys()}"
    )

    payments.crash()
    decision = coordinator.run("tx-3", {"orders": {"alice": -10}, "payments": {"bob": 10}})
    print(f"tx-3 payments down during phase 1   -> {decision.value}; timeout counts as a NO vote")
    payments.recover(coordinator)

    coordinator.crash_at(CrashPoint.BEFORE_DECISION_LOGGED)
    try:
        coordinator.run("tx-4", {"orders": {"alice": -10}, "payments": {"bob": 10}})
    except NodeDownError as exc:
        print(f"tx-4 {exc}")
    print(
        f"     orders in doubt={orders.in_doubt()} holding {orders.locked_keys()}; "
        f"payments in doubt={payments.in_doubt()}"
    )
    vote = orders.prepare("tx-5", {"alice": -1})
    print(f"     tx-5 touching alice meanwhile   -> votes {vote.value}: blocked by tx-4's lock")
    resolved = coordinator.recover()
    print(f"     coordinator recovers, presumed abort for {resolved}; {balances()}")

    coordinator.crash_at(CrashPoint.AFTER_DECISION_LOGGED)
    try:
        coordinator.run("tx-6", {"orders": {"alice": -20}, "payments": {"bob": 20}})
    except NodeDownError as exc:
        print(f"tx-6 {exc}")
    print(f"     log says {coordinator.decision_of('tx-6')}, nobody was told; pending={coordinator.pending()}")
    coordinator.recover()
    print(f"     coordinator recovers, replays commit -> {balances()}")

    payments.crash_after_vote()
    decision = coordinator.run("tx-7", {"orders": {"alice": -5}, "payments": {"bob": 5}})
    print(
        f"tx-7 payments dies right after YES   -> {decision.value}; "
        f"payments in doubt={payments.in_doubt()}, bob={payments.get('bob')}"
    )
    payments.recover(coordinator)
    print(f"     payments recovers and asks the coordinator -> {balances()}")


if __name__ == "__main__":
    main()
