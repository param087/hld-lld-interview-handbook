"""Idempotency-key store with in-progress and completed states, a TTL and claim tokens.

What the module demonstrates, in the order an interviewer asks about it:

* ``IdempotencyStore.begin`` is the single decision point for a retried POST: ``NEW`` (the
  caller owns the key and must run the handler), ``REPLAY`` (the key completed earlier, here is
  the stored response), ``IN_PROGRESS`` (a twin request is still running: answer 409) or
  ``MISMATCH`` (same key, different payload: answer 422).
* Every record carries a fingerprint of the request, a state and an expiry. A completed record
  lives for ``ttl`` seconds. A claim older than ``in_progress_ttl`` belongs to a worker that
  died mid-flight and may be taken over; the claim token stops that worker's late ``complete``
  from overwriting the survivor's response.
* ``IdempotentHandler.execute`` wraps a handler in the whole dance: at-least-once delivery in
  front of it becomes effectively-once processing behind it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from common import Clock, ConflictError, FakeClock, HandbookError, ValidationError


# --8<-- [start:records]
class Outcome(StrEnum):
    NEW = "new"  # the caller owns the key: run the handler, then complete()
    REPLAY = "replay"  # completed earlier: return the stored response, run nothing
    IN_PROGRESS = "in_progress"  # a twin request is running: 409, retry later
    MISMATCH = "mismatch"  # same key, different payload: 422, a client bug


class RecordState(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


@dataclass(slots=True)
class Record:
    key: str
    fingerprint: str
    state: RecordState
    token: int  # claim token: a new one per takeover, so a stale worker cannot complete
    expires_at: float
    response: Response | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    outcome: Outcome
    token: int = 0
    response: Response | None = None


def fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash of the canonical JSON form, so a key cannot be silently reused for another request."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# --8<-- [end:records]


# --8<-- [start:store]
class IdempotencyStore:
    """Records keyed by ``scope:key`` (scope = tenant or user), so two clients cannot collide.

    ``_lock`` guards ``_records`` and ``_next_token``. ``begin`` is one critical section, which
    is what makes two simultaneous twins resolve to exactly one owner and one 409.
    """

    def __init__(self, clock: Clock, ttl: float = 24 * 3600, in_progress_ttl: float = 30.0) -> None:
        if ttl <= 0 or in_progress_ttl <= 0:
            raise ValidationError("ttl and in_progress_ttl must be positive")
        self._clock = clock
        self._ttl = ttl
        self._in_progress_ttl = in_progress_ttl
        self._records: dict[str, Record] = {}
        self._next_token = 1
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def begin(self, scope: str, key: str, request_fingerprint: str) -> Claim:
        """Claim the key, or say why the caller must not run the handler."""
        if not key:
            raise ValidationError("idempotency key must be non-empty")
        full_key = f"{scope}:{key}"
        now = self._clock.now()
        with self._lock:
            record = self._records.get(full_key)
            if record is not None and record.expires_at <= now:
                del self._records[full_key]  # completed and past its TTL, or an abandoned claim
                record = None
            if record is None:
                token = self._next_token
                self._next_token += 1
                expires_at = now + self._in_progress_ttl
                self._records[full_key] = Record(
                    full_key, request_fingerprint, RecordState.IN_PROGRESS, token, expires_at
                )
                return Claim(Outcome.NEW, token)
            if record.fingerprint != request_fingerprint:
                return Claim(Outcome.MISMATCH)
            if record.state is RecordState.COMPLETED:
                return Claim(Outcome.REPLAY, record.token, record.response)
            return Claim(Outcome.IN_PROGRESS)

    def complete(self, scope: str, key: str, token: int, response: Response) -> None:
        """Store the response; rejected when the claim was taken over by another worker."""
        full_key = f"{scope}:{key}"
        with self._lock:
            record = self._records.get(full_key)
            if record is None or record.token != token:
                raise ConflictError(f"{full_key}: claim {token} is no longer current")
            record.state = RecordState.COMPLETED
            record.response = response
            record.expires_at = self._clock.now() + self._ttl

    def release(self, scope: str, key: str, token: int) -> None:
        """Drop a claim whose handler did nothing, so the client's retry is NEW again."""
        full_key = f"{scope}:{key}"
        with self._lock:
            record = self._records.get(full_key)
            if record is not None and record.token == token and record.state is RecordState.IN_PROGRESS:
                del self._records[full_key]

    def purge_expired(self) -> int:
        """What a background sweeper (or Redis TTL) does: forget expired records, return the count."""
        now = self._clock.now()
        with self._lock:
            expired = [key for key, record in self._records.items() if record.expires_at <= now]
            for key in expired:
                del self._records[key]
            return len(expired)


# --8<-- [end:store]


# --8<-- [start:handler]
Handler = Callable[[Mapping[str, Any]], Response]


class IdempotentHandler:
    """Wraps a handler with side effects (charge a card, create an order) in the key protocol.

    A ``HandbookError`` from the handler is a deterministic business failure: it is stored and
    replayed like any other response, because a retry would fail the same way. Any other
    exception means "unknown whether the side effect happened": the claim is kept, twins get
    409 until ``in_progress_ttl`` passes, then a retry may take the claim over and run the
    handler again, which is why the handler itself must tolerate a second run.
    """

    def __init__(self, store: IdempotencyStore, handler: Handler) -> None:
        self._store = store
        self._handler = handler

    def execute(self, scope: str, key: str, payload: Mapping[str, Any]) -> tuple[Outcome, Response]:
        claim = self._store.begin(scope, key, fingerprint(payload))
        if claim.outcome is Outcome.REPLAY and claim.response is not None:
            return claim.outcome, claim.response
        if claim.outcome is Outcome.IN_PROGRESS:
            return claim.outcome, Response(409, {"error": "a request with this key is in progress"})
        if claim.outcome is Outcome.MISMATCH:
            return claim.outcome, Response(422, {"error": "key reused with a different payload"})
        try:
            response = self._handler(payload)
        except HandbookError as exc:
            response = Response(422, {"error": str(exc)})
        self._store.complete(scope, key, claim.token, response)
        return Outcome.NEW, response


# --8<-- [end:handler]


def main() -> None:
    clock = FakeClock(start=1_000_000.0)
    store = IdempotencyStore(clock, ttl=24 * 3600, in_progress_ttl=30.0)
    ledger: list[int] = []

    def charge(payload: Mapping[str, Any]) -> Response:
        if payload["amount"] > 500:
            raise ValidationError("card declined")
        ledger.append(payload["amount"])
        return Response(201, {"charge_id": f"ch_{len(ledger)}", "amount": payload["amount"]})

    handler = IdempotentHandler(store, charge)
    payload = {"amount": 120, "currency": "USD"}

    def show(label: str, outcome: Outcome, response: Response) -> None:
        print(f"{label:<44} {outcome.value:<12} {response.status} {dict(response.body)}  charges={ledger}")

    show("POST charge key=k1", *handler.execute("acct-7", "k1", payload))
    show("retry key=k1, same payload", *handler.execute("acct-7", "k1", payload))
    show("retry key=k1, amount changed", *handler.execute("acct-7", "k1", {**payload, "amount": 121}))
    show("POST key=k1 from another account", *handler.execute("acct-8", "k1", payload))
    show("POST key=k2, card declined", *handler.execute("acct-7", "k2", {**payload, "amount": 900}))
    show("retry key=k2", *handler.execute("acct-7", "k2", {**payload, "amount": 900}))

    claim = store.begin("acct-7", "k3", fingerprint(payload))  # a worker takes k3 and stalls
    show("POST key=k3 while a twin is in flight", *handler.execute("acct-7", "k3", payload))
    clock.advance(31)
    show("retry key=k3 after 31 s, claim expired", *handler.execute("acct-7", "k3", payload))
    try:
        store.complete("acct-7", "k3", claim.token, Response(201, {"charge_id": "late"}))
    except ConflictError as exc:
        print(f"{'stalled worker completes late':<44} rejected: {exc}")

    clock.advance(24 * 3600)
    print(f"24 h later: purge_expired removed {store.purge_expired()} records, {len(store)} left")
    show("retry key=k1 after the TTL", *handler.execute("acct-7", "k1", payload))


if __name__ == "__main__":
    main()
