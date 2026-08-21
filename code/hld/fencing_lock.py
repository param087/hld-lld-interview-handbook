"""Lease-based distributed lock with fencing tokens, and a store that checks them.

What the module demonstrates, in the order an interviewer asks about it:

* ``LeaseLockService.acquire`` grants a lease on a resource for ``ttl`` seconds and stamps it
  with a fencing token from a counter that only ever increases, the way ZooKeeper's zxid or
  etcd's revision does. ``renew`` extends a lease the caller still holds; once a lease has
  expired anyone may take the resource.
* ``FencedStore.write`` accepts a write only if its token is at least the highest token it
  has already accepted for that key. A client that held the lock, paused past its TTL and
  woke up convinced it still holds it is rejected: the failure a lock alone cannot prevent.
* ``main`` replays that scenario with and without the token check.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from common import Clock, ConflictError, FakeClock, ValidationError


class FencingError(ConflictError):
    """The write carried a fencing token older than one the store has already accepted."""


# --8<-- [start:lease]
@dataclass(frozen=True, slots=True)
class Lease:
    """Proof of holding ``resource`` until ``expires_at``; ``token`` orders every grant ever made."""

    resource: str
    owner: str
    token: int
    expires_at: float

    def remaining(self, now: float) -> float:
        """Seconds left; the holder must stop touching the resource before this reaches zero."""
        return self.expires_at - now


class LeaseLockService:
    """A lock service in the style of Chubby, ZooKeeper or etcd leases.

    ``_lock`` guards ``_leases`` and ``_next_token``. Tokens come from one counter for the
    whole service, so any two grants, on any resources, are strictly ordered, and a store only
    has to remember the highest token it has accepted per key.
    """

    def __init__(self, clock: Clock, ttl: float = 10.0) -> None:
        if ttl <= 0:
            raise ValidationError("ttl must be positive")
        self._clock = clock
        self._ttl = ttl
        self._leases: dict[str, Lease] = {}
        self._next_token = 0
        self._lock = threading.Lock()

    def acquire(self, resource: str, owner: str) -> Lease | None:
        """Grant a lease if ``resource`` is free or its lease has expired; ``None`` while held."""
        if not resource or not owner:
            raise ValidationError("resource and owner must be non-empty")
        with self._lock:
            now = self._clock.now()
            current = self._leases.get(resource)
            if current is not None and current.expires_at > now:
                return None
            self._next_token += 1
            lease = Lease(resource, owner, self._next_token, now + self._ttl)
            self._leases[resource] = lease
            return lease

    def renew(self, lease: Lease) -> Lease | None:
        """The holder's keep-alive: extend a lease that is still current, same token."""
        with self._lock:
            now = self._clock.now()
            current = self._leases.get(lease.resource)
            if current is None or current.token != lease.token or current.expires_at <= now:
                return None  # expired or taken over: the caller has lost the lock
            renewed = Lease(lease.resource, lease.owner, lease.token, now + self._ttl)
            self._leases[lease.resource] = renewed
            return renewed

    def release(self, lease: Lease) -> bool:
        """Free the resource early; a stale lease (token no longer current) releases nothing."""
        with self._lock:
            current = self._leases.get(lease.resource)
            if current is None or current.token != lease.token:
                return False
            del self._leases[lease.resource]
            return True

    def holder(self, resource: str) -> Lease | None:
        """The unexpired lease on ``resource``, if any."""
        with self._lock:
            current = self._leases.get(resource)
            if current is None or current.expires_at <= self._clock.now():
                return None
            return current


# --8<-- [end:lease]


# --8<-- [start:store]
class FencedStore:
    """A storage service that honours fencing tokens.

    ``_lock`` guards ``_values`` and ``rejected``. A write whose token is lower than the
    highest already accepted for that key comes from a client whose lease ended, however
    convinced that client is that it still holds the lock. ``check_tokens=False`` is the
    store without the check, for contrast: it takes the stale write and loses the newer one.
    """

    def __init__(self, check_tokens: bool = True) -> None:
        self._check_tokens = check_tokens
        self._values: dict[str, tuple[int, str]] = {}  # key -> (highest token, value)
        self._lock = threading.Lock()
        self.rejected = 0

    def write(self, key: str, value: str, token: int) -> None:
        with self._lock:
            current = self._values.get(key)
            if self._check_tokens and current is not None and token < current[0]:
                self.rejected += 1
                raise FencingError(
                    f"write to {key!r} with token {token} rejected: token {current[0]} already seen"
                )
            self._values[key] = (token, value)

    def read(self, key: str) -> str | None:
        with self._lock:
            current = self._values.get(key)
            return None if current is None else current[1]

    def token_of(self, key: str) -> int | None:
        with self._lock:
            current = self._values.get(key)
            return None if current is None else current[0]


# --8<-- [end:store]


def main() -> None:
    clock = FakeClock(start=0.0)
    service = LeaseLockService(clock, ttl=10.0)
    print("lease lock with ttl 10 s; clients A and B; one fenced store")

    def replay(store: FencedStore, label: str) -> None:
        clock.set(0.0)
        lease_a = service.acquire("job:42", "A")
        assert lease_a is not None
        store.write("result", "A-first", lease_a.token)
        print(
            f"[{label}] t= 0 s  A acquires job:42 -> token {lease_a.token}, expires t=10 s; "
            f"A writes result=A-first (token {lease_a.token}) ok"
        )
        clock.advance(1)
        print(f"[{label}] t= 1 s  B tries job:42 -> {'busy' if service.acquire('job:42', 'B') is None else 'granted'}")
        clock.advance(11)  # A stalls for 12 s: a GC pause, a VM migration, a network stall
        lease_b = service.acquire("job:42", "B")
        assert lease_b is not None
        store.write("result", "B-first", lease_b.token)
        print(
            f"[{label}] t=12 s  A has been paused for 11 s; lease expired, B acquires -> token "
            f"{lease_b.token}; B writes result=B-first ok"
        )
        clock.advance(1)
        try:
            store.write("result", "A-second", lease_a.token)
            outcome = "accepted"
        except FencingError as exc:
            outcome = f"rejected ({exc})"
        print(
            f"[{label}] t=13 s  A wakes up, lease.remaining={lease_a.remaining(clock.now()):+.0f} s, "
            f"writes result=A-second with token {lease_a.token} -> {outcome}"
        )
        print(f"[{label}]        store: result={store.read('result')} (token {store.token_of('result')})")
        service.release(lease_b)

    replay(FencedStore(), "fenced")
    replay(FencedStore(check_tokens=False), "unfenced")
    lease = service.acquire("job:42", "C")
    assert lease is not None
    clock.advance(6)
    renewed = service.renew(lease)
    assert renewed is not None
    print(
        f"C acquires -> token {lease.token} (tokens are never reused); renew at t={clock.now():.0f} s "
        f"-> expires t={renewed.expires_at:.0f} s; release -> {service.release(renewed)}; "
        f"stale release -> {service.release(lease)}"
    )


if __name__ == "__main__":
    main()
