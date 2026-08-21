"""The subscription service, tested the way this handbook argues you should test.

Every test is arrange-act-assert with blank lines between the three parts, time
comes from a ``FakeClock``, the doubles are fakes rather than mocks, and the one
concurrency test forces its race with a ``Barrier`` instead of sleeping.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from common import (
    FakeClock,
    InvalidStateError,
    Money,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)
from fundamentals.testing_examples import (
    SECONDS_PER_DAY,
    FakePaymentGateway,
    InMemorySubscriptionRepository,
    PaymentDeclinedError,
    PaymentGateway,
    Plan,
    RenewalTooEarlyError,
    SubscriptionService,
    SubscriptionStatus,
    configured_currency,
)

PRICES = {Plan.MONTHLY: Money.of("9.99"), Plan.ANNUAL: Money.of("99.00")}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=0.0)


@pytest.fixture
def gateway() -> FakePaymentGateway:
    return FakePaymentGateway()


@pytest.fixture
def repository() -> InMemorySubscriptionRepository:
    return InMemorySubscriptionRepository()


@pytest.fixture
def service(
    repository: InMemorySubscriptionRepository, gateway: FakePaymentGateway, clock: FakeClock
) -> SubscriptionService:
    """One assembled use case. Small fixtures compose; one big `setup` does not."""
    return SubscriptionService(repository, gateway, clock, SequentialIdGenerator("sub"), PRICES)


@pytest.mark.parametrize(("plan", "days"), [(Plan.MONTHLY, 30), (Plan.ANNUAL, 365)])
def test_subscribing_sets_the_expiry_from_the_injected_clock(
    service: SubscriptionService, clock: FakeClock, plan: Plan, days: int
) -> None:
    subscription = service.subscribe("cus-ada", plan)

    assert subscription.days_remaining(clock.now()) == days
    assert subscription.expires_at == days * SECONDS_PER_DAY  # exact, because the clock is ours


@pytest.mark.parametrize("customer_id", ["", "   "])
def test_a_blank_customer_id_is_rejected_before_anything_is_stored(
    service: SubscriptionService, repository: InMemorySubscriptionRepository, customer_id: str
) -> None:
    with pytest.raises(ValidationError, match="customer id"):
        service.subscribe(customer_id, Plan.MONTHLY)

    with pytest.raises(NotFoundError):
        repository.get("sub-1")  # the guard clause ran before the id generator did


def test_a_plan_without_a_price_is_rejected(
    repository: InMemorySubscriptionRepository, gateway: FakePaymentGateway, clock: FakeClock
) -> None:
    monthly_only = SubscriptionService(
        repository, gateway, clock, SequentialIdGenerator("sub"), {Plan.MONTHLY: Money.of("9.99")}
    )

    with pytest.raises(ValidationError, match="no price"):
        monthly_only.subscribe("cus-ada", Plan.ANNUAL)


def test_renewal_is_refused_outside_the_window(service: SubscriptionService) -> None:
    subscription = service.subscribe("cus-ada", Plan.MONTHLY)

    with pytest.raises(RenewalTooEarlyError, match="30 days left"):
        service.renew(subscription.id)


def test_renewal_inside_the_window_extends_from_the_old_expiry(
    service: SubscriptionService, clock: FakeClock, gateway: FakePaymentGateway
) -> None:
    subscription = service.subscribe("cus-ada", Plan.MONTHLY)
    clock.advance(25 * SECONDS_PER_DAY)

    renewed = service.renew(subscription.id)

    assert renewed.days_remaining(clock.now()) == 35  # the 5 unused days are not lost
    assert gateway.charges == [("cus-ada", Money.of("9.99"))]


def test_a_declined_card_leaves_the_subscription_exactly_as_it_was(
    service: SubscriptionService,
    repository: InMemorySubscriptionRepository,
    gateway: FakePaymentGateway,
    clock: FakeClock,
) -> None:
    subscription = service.subscribe("cus-ada", Plan.MONTHLY)
    clock.advance(25 * SECONDS_PER_DAY)
    gateway.decline_next()

    with pytest.raises(PaymentDeclinedError):
        service.renew(subscription.id)

    assert repository.get(subscription.id) == subscription  # frozen value, unchanged
    assert gateway.charges == []


def test_a_cancelled_subscription_cannot_be_renewed_or_cancelled_twice(
    service: SubscriptionService, clock: FakeClock
) -> None:
    subscription = service.subscribe("cus-ada", Plan.MONTHLY)
    clock.advance(25 * SECONDS_PER_DAY)

    cancelled = service.cancel(subscription.id)

    assert cancelled.status is SubscriptionStatus.CANCELLED
    with pytest.raises(InvalidStateError, match="not active"):
        service.renew(subscription.id)
    with pytest.raises(InvalidStateError, match="already cancelled"):
        service.cancel(subscription.id)


def test_an_unknown_subscription_raises_the_domain_error_not_a_key_error(
    service: SubscriptionService,
) -> None:
    with pytest.raises(NotFoundError, match="sub-404"):
        service.renew("sub-404")


def test_currency_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one place ``monkeypatch`` earns its keep: an input that cannot be injected."""
    assert configured_currency() == "USD"

    monkeypatch.setenv("HANDBOOK_CURRENCY", "EUR")

    assert configured_currency() == "EUR"


def test_the_gateway_is_charged_once_per_renewal(
    repository: InMemorySubscriptionRepository, clock: FakeClock
) -> None:
    """The narrow case for a mock: when the *call* is the behaviour under test.

    ``spec=PaymentGateway`` makes the double fail if the Protocol is renamed, which
    is the failure mode that makes bare ``Mock()`` a liability.
    """
    charger = mock.Mock(spec=PaymentGateway)
    charger.charge.return_value = "rcpt-1"
    service = SubscriptionService(repository, charger, clock, SequentialIdGenerator("sub"), PRICES)
    subscription = service.subscribe("cus-ada", Plan.MONTHLY)
    clock.advance(25 * SECONDS_PER_DAY)

    service.renew(subscription.id)

    charger.charge.assert_called_once_with("cus-ada", Money.of("9.99"))


def test_a_fake_shared_across_threads_records_every_charge_exactly_once(
    gateway: FakePaymentGateway,
) -> None:
    """A fake used from a concurrency test needs its own lock, or the test lies."""
    workers = 8
    start = threading.Barrier(workers)

    def charge(index: int) -> str:
        start.wait(timeout=2.0)  # every thread hits `charge` at the same moment
        return gateway.charge(f"cus-{index}", Money.of("9.99"))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        receipts = [f.result(timeout=5.0) for f in [pool.submit(charge, i) for i in range(workers)]]

    assert len(set(receipts)) == workers
    assert len(gateway.charges) == workers
