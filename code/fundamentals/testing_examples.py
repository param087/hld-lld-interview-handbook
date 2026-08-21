"""Clean code and testable code, on one small subscription domain.

The split that makes the tests fast is the one worth copying: ``Subscription``
is a frozen value that only does arithmetic, ``SubscriptionService`` is the use
case, and everything that touches the outside world — payments, storage, the
clock, IDs, the environment — arrives through a constructor argument. Nothing
here calls ``time.time()``, so every test is deterministic by construction.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from common import (
    Clock,
    ConflictError,
    FakeClock,
    IdGenerator,
    InvalidStateError,
    Money,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)

SECONDS_PER_DAY = 86_400
RENEWAL_WINDOW_DAYS = 7


# --8<-- [start:errors]
class PaymentDeclinedError(ConflictError):
    """The gateway said no. A distinct type, because callers retry this and not much else."""


class RenewalTooEarlyError(InvalidStateError):
    """Renewal outside the window. Named for the rule it enforces, not for where it is raised."""


# --8<-- [end:errors]
# --8<-- [start:domain]
class Plan(StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


DURATION_DAYS: Mapping[Plan, int] = {Plan.MONTHLY: 30, Plan.ANNUAL: 365}


@dataclass(frozen=True, slots=True)
class Subscription:
    """A value, not a record: every transition returns a new one.

    Frozen means a caller cannot quietly reach in and move ``expires_at``, and
    an object handed to another thread cannot change under it. ``now`` is a
    parameter on every time-dependent method, so nothing here reads a clock.
    """

    id: str
    customer_id: str
    plan: Plan
    expires_at: float
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE

    def days_remaining(self, now: float) -> int:
        return max(0, int((self.expires_at - now) // SECONDS_PER_DAY))

    def is_renewable(self, now: float) -> bool:
        return self.status is SubscriptionStatus.ACTIVE and self.days_remaining(now) <= RENEWAL_WINDOW_DAYS

    def renewed(self, now: float) -> Subscription:
        """Extend from the later of the old expiry and now, so an early renewal loses no days."""
        base = max(self.expires_at, now)
        return replace(self, expires_at=base + DURATION_DAYS[self.plan] * SECONDS_PER_DAY)

    def cancelled(self) -> Subscription:
        return replace(self, status=SubscriptionStatus.CANCELLED)


# --8<-- [end:domain]
# --8<-- [start:boundary]
class PaymentGateway(Protocol):
    """The IO boundary. One method, so a fake is five lines and a mock is unnecessary."""

    def charge(self, customer_id: str, amount: Money) -> str: ...


class SubscriptionRepository(Protocol):
    def save(self, subscription: Subscription) -> None: ...

    def get(self, subscription_id: str) -> Subscription: ...


class InMemorySubscriptionRepository:
    """A fake, not a mock: it really stores and really returns, so tests assert on outcomes."""

    def __init__(self) -> None:
        self._rows: dict[str, Subscription] = {}

    def save(self, subscription: Subscription) -> None:
        self._rows[subscription.id] = subscription

    def get(self, subscription_id: str) -> Subscription:
        try:  # EAFP: one dict lookup, and the miss is the exceptional path
            return self._rows[subscription_id]
        except KeyError:
            raise NotFoundError(f"no subscription {subscription_id!r}") from None


class FakePaymentGateway:
    """Records what it was asked to do, and can be told to refuse the next charge.

    The lock is not ceremony: a fake used from a concurrency test is production
    code for the test suite, and an unguarded ``list.append`` plus counter would
    make the test that exercises threads flaky for reasons that are your fault.
    """

    def __init__(self) -> None:
        self.charges: list[tuple[str, Money]] = []
        self._lock = threading.Lock()
        self._decline_next = False

    def decline_next(self) -> None:
        self._decline_next = True

    def charge(self, customer_id: str, amount: Money) -> str:
        with self._lock:
            if self._decline_next:
                self._decline_next = False
                raise PaymentDeclinedError(f"payment for {customer_id} was declined")
            self.charges.append((customer_id, amount))
            return f"rcpt-{len(self.charges)}"


# --8<-- [end:boundary]
# --8<-- [start:environment]
def configured_currency() -> str:
    """Read the process environment — the one input here that cannot be injected.

    Keeping such reads in one named function at the edge means exactly one test
    needs ``monkeypatch.setenv`` and the rest of the suite stays free of it.
    """
    return os.environ.get("HANDBOOK_CURRENCY", "USD")


# --8<-- [end:environment]
# --8<-- [start:service]
class SubscriptionService:
    """The use case. Guard clauses first, one decision per line, no nesting past two levels."""

    def __init__(
        self,
        repository: SubscriptionRepository,
        gateway: PaymentGateway,
        clock: Clock,
        ids: IdGenerator,
        prices: Mapping[Plan, Money],
    ) -> None:
        self._repository = repository
        self._gateway = gateway
        self._clock = clock
        self._ids = ids
        self._prices = dict(prices)

    def subscribe(self, customer_id: str, plan: Plan) -> Subscription:
        if not customer_id.strip():
            raise ValidationError("a subscription needs a customer id")
        if plan not in self._prices:
            raise ValidationError(f"no price configured for the {plan} plan")

        now = self._clock.now()
        subscription = Subscription(
            id=self._ids.next_id(),
            customer_id=customer_id,
            plan=plan,
            expires_at=now + DURATION_DAYS[plan] * SECONDS_PER_DAY,
        )
        self._repository.save(subscription)
        return subscription

    def renew(self, subscription_id: str) -> Subscription:
        """Charge, then commit. A declined card must leave the subscription untouched."""
        current = self._repository.get(subscription_id)
        now = self._clock.now()
        self._guard_renewable(current, now)

        self._gateway.charge(current.customer_id, self._prices[current.plan])

        renewed = current.renewed(now)
        self._repository.save(renewed)
        return renewed

    def cancel(self, subscription_id: str) -> Subscription:
        current = self._repository.get(subscription_id)
        if current.status is SubscriptionStatus.CANCELLED:
            raise InvalidStateError(f"{current.id} is already cancelled")
        cancelled = current.cancelled()
        self._repository.save(cancelled)
        return cancelled

    @staticmethod
    def _guard_renewable(subscription: Subscription, now: float) -> None:
        """One guard, one message. Named so the caller reads as a sentence."""
        if subscription.status is not SubscriptionStatus.ACTIVE:
            raise InvalidStateError(f"{subscription.id} is {subscription.status}, not active")
        if not subscription.is_renewable(now):
            raise RenewalTooEarlyError(
                f"{subscription.id} has {subscription.days_remaining(now)} days left"
                f" and renews in the last {RENEWAL_WINDOW_DAYS}"
            )


# --8<-- [end:service]


def main() -> None:
    clock = FakeClock(start=0.0)
    gateway = FakePaymentGateway()
    repository = InMemorySubscriptionRepository()
    prices = {Plan.MONTHLY: Money.of("9.99"), Plan.ANNUAL: Money.of("99.00")}
    service = SubscriptionService(repository, gateway, clock, SequentialIdGenerator("sub"), prices)

    subscription = service.subscribe("cus-ada", Plan.MONTHLY)
    print("--- day 0: Ada subscribes to the monthly plan ---")
    print(f"{subscription.id} expires in {subscription.days_remaining(clock.now())} days, status {subscription.status}")

    print("--- renewal is refused while 30 days remain ---")
    try:
        service.renew(subscription.id)
    except RenewalTooEarlyError as exc:
        print(f"refused: {exc}")

    clock.advance(25 * SECONDS_PER_DAY)
    gateway.decline_next()
    print("--- day 25: the card is declined, so nothing changes ---")
    try:
        service.renew(subscription.id)
    except PaymentDeclinedError as exc:
        print(f"refused: {exc}")
    unchanged = repository.get(subscription.id)
    print(f"{unchanged.id} still expires in {unchanged.days_remaining(clock.now())} days, status {unchanged.status}")

    renewed = service.renew(subscription.id)
    print("--- the retry succeeds and extends from the old expiry ---")
    print(
        f"{renewed.id} now expires in {renewed.days_remaining(clock.now())} days"
        f" after {len(gateway.charges)} charge of {gateway.charges[0][1]}"
    )

    service.cancel(subscription.id)
    print("--- cancelling is terminal ---")
    try:
        service.renew(subscription.id)
    except InvalidStateError as exc:
        print(f"refused: {exc}")
    print(f"currency read from the environment: {configured_currency()}")


if __name__ == "__main__":
    main()
