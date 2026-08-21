import threading
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

import pytest

from common import ConflictError, FakeClock, ValidationError
from hld.idempotency_store import (
    IdempotencyStore,
    IdempotentHandler,
    Outcome,
    Response,
    fingerprint,
)

PAYLOAD = {"amount": 120, "currency": "USD"}


class Charger:
    """A handler that counts its calls, so tests can prove it ran exactly once."""

    def __init__(self) -> None:
        self.calls = 0
        self.gate: threading.Event | None = None
        self._lock = threading.Lock()

    def __call__(self, payload: Mapping[str, Any]) -> Response:
        if payload["amount"] > 500:
            raise ValidationError("card declined")
        if self.gate is not None:
            self.gate.wait()
        with self._lock:
            self.calls += 1
            return Response(201, {"charge_id": f"ch_{self.calls}"})


def make() -> tuple[FakeClock, IdempotencyStore, Charger, IdempotentHandler]:
    clock = FakeClock(start=1_000.0)
    store = IdempotencyStore(clock, ttl=3600, in_progress_ttl=30)
    charger = Charger()
    return clock, store, charger, IdempotentHandler(store, charger)


def test_first_call_runs_the_handler_and_retries_replay_the_stored_response() -> None:
    _, _, charger, handler = make()
    first = handler.execute("acct-1", "k1", PAYLOAD)
    second = handler.execute("acct-1", "k1", PAYLOAD)
    assert first == (Outcome.NEW, Response(201, {"charge_id": "ch_1"}))
    assert second == (Outcome.REPLAY, Response(201, {"charge_id": "ch_1"}))
    assert charger.calls == 1


def test_reusing_a_key_with_a_different_payload_is_rejected() -> None:
    _, _, charger, handler = make()
    handler.execute("acct-1", "k1", PAYLOAD)
    outcome, response = handler.execute("acct-1", "k1", {**PAYLOAD, "amount": 121})
    assert (outcome, response.status) == (Outcome.MISMATCH, 422)
    assert charger.calls == 1


def test_keys_are_scoped_per_client() -> None:
    _, _, charger, handler = make()
    handler.execute("acct-1", "k1", PAYLOAD)
    outcome, _ = handler.execute("acct-2", "k1", PAYLOAD)
    assert outcome is Outcome.NEW
    assert charger.calls == 2


def test_deterministic_business_failures_are_stored_and_replayed() -> None:
    _, _, charger, handler = make()
    declined = {**PAYLOAD, "amount": 900}
    first = handler.execute("acct-1", "k2", declined)
    second = handler.execute("acct-1", "k2", declined)
    assert first == (Outcome.NEW, Response(422, {"error": "card declined"}))
    assert second == (Outcome.REPLAY, Response(422, {"error": "card declined"}))
    assert charger.calls == 0


def test_a_twin_in_flight_sees_in_progress_until_the_owner_completes() -> None:
    _, store, _, _ = make()
    fp = fingerprint(PAYLOAD)
    owner = store.begin("acct-1", "k3", fp)
    assert owner.outcome is Outcome.NEW
    assert store.begin("acct-1", "k3", fp).outcome is Outcome.IN_PROGRESS
    store.complete("acct-1", "k3", owner.token, Response(201, {"charge_id": "ch_9"}))
    replay = store.begin("acct-1", "k3", fp)
    assert (replay.outcome, replay.response) == (Outcome.REPLAY, Response(201, {"charge_id": "ch_9"}))


def test_abandoned_claim_is_taken_over_and_the_stale_worker_cannot_complete() -> None:
    clock, store, _, _ = make()
    fp = fingerprint(PAYLOAD)
    stalled = store.begin("acct-1", "k4", fp)
    clock.advance(29)
    assert store.begin("acct-1", "k4", fp).outcome is Outcome.IN_PROGRESS
    clock.advance(2)  # 31 s: the claim is older than in_progress_ttl
    survivor = store.begin("acct-1", "k4", fp)
    assert survivor.outcome is Outcome.NEW
    assert survivor.token > stalled.token
    with pytest.raises(ConflictError):
        store.complete("acct-1", "k4", stalled.token, Response(201, {"charge_id": "late"}))
    store.complete("acct-1", "k4", survivor.token, Response(201, {"charge_id": "ch_ok"}))
    assert store.begin("acct-1", "k4", fp).response == Response(201, {"charge_id": "ch_ok"})


def test_release_frees_the_key_but_ignores_stale_tokens_and_completed_records() -> None:
    _, store, _, _ = make()
    fp = fingerprint(PAYLOAD)
    claim = store.begin("acct-1", "k5", fp)
    store.release("acct-1", "k5", claim.token + 1)  # wrong token: nothing happens
    assert store.begin("acct-1", "k5", fp).outcome is Outcome.IN_PROGRESS
    store.release("acct-1", "k5", claim.token)
    again = store.begin("acct-1", "k5", fp)
    assert again.outcome is Outcome.NEW
    store.complete("acct-1", "k5", again.token, Response(201, {}))
    store.release("acct-1", "k5", again.token)  # completed records are never released
    assert store.begin("acct-1", "k5", fp).outcome is Outcome.REPLAY


def test_completed_records_expire_after_the_ttl_and_purge_counts_them() -> None:
    clock, store, charger, handler = make()
    handler.execute("acct-1", "k1", PAYLOAD)
    handler.execute("acct-1", "k2", PAYLOAD)
    clock.advance(3599)
    assert handler.execute("acct-1", "k1", PAYLOAD)[0] is Outcome.REPLAY
    clock.advance(2)
    assert handler.execute("acct-1", "k1", PAYLOAD)[0] is Outcome.NEW  # forgotten: runs again
    assert charger.calls == 3
    assert store.purge_expired() == 1  # k2 expired; k1 was just refreshed
    assert len(store) == 1


def test_fingerprint_ignores_key_order_and_validation_errors() -> None:
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    clock = FakeClock()
    with pytest.raises(ValidationError):
        IdempotencyStore(clock, ttl=0)
    with pytest.raises(ValidationError):
        IdempotencyStore(clock, in_progress_ttl=-1)
    with pytest.raises(ValidationError):
        IdempotencyStore(clock).begin("acct-1", "", "fp")


def test_concurrent_twins_run_the_handler_exactly_once() -> None:
    _, _, charger, handler = make()
    charger.gate = threading.Event()  # the owner blocks until every twin has been answered
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(handler.execute, "acct-1", "k-race", PAYLOAD) for _ in range(32)]
        pending = set(futures)
        while len(pending) > 1:
            _, pending = wait(pending, return_when=FIRST_COMPLETED)
        charger.gate.set()
        results = [f.result() for f in futures]
    outcomes = [outcome for outcome, _ in results]
    assert charger.calls == 1
    assert outcomes.count(Outcome.NEW) == 1
    assert outcomes.count(Outcome.IN_PROGRESS) == 31
    assert all(r.status == 409 for o, r in results if o is Outcome.IN_PROGRESS)
    with ThreadPoolExecutor(max_workers=8) as pool:
        replays = list(pool.map(lambda _: handler.execute("acct-1", "k-race", PAYLOAD), range(16)))
    assert all(o is Outcome.REPLAY and r == Response(201, {"charge_id": "ch_1"}) for o, r in replays)
    assert charger.calls == 1
