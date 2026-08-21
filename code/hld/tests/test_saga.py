from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, NotFoundError, ValidationError
from hld.saga import (
    InventoryService,
    OrchestratorCrash,
    OrderService,
    PaymentService,
    SagaContext,
    SagaLog,
    SagaOrchestrator,
    SagaState,
    ShippingService,
    Step,
    StepKind,
    TransientError,
    order_saga,
)

Bench = tuple[SagaOrchestrator, SagaLog, OrderService, InventoryService, PaymentService, ShippingService]


def bench(stock: int = 10, max_attempts: int = 3) -> Bench:
    orders, inventory = OrderService(), InventoryService({"sku-1": stock})
    payments, shipping, log = PaymentService(credit_limit=500), ShippingService(), SagaLog()
    saga = order_saga(orders, inventory, payments, shipping, log, max_attempts=max_attempts)
    return saga, log, orders, inventory, payments, shipping


def order(order_id: str, amount: int = 120, qty: int = 1) -> SagaContext:
    return {"order_id": order_id, "sku": "sku-1", "qty": qty, "amount": amount}


def test_happy_path_runs_every_step_once_in_order() -> None:
    saga, log, orders, inventory, payments, _ = bench()
    assert saga.start("o-1", order("o-1")) is SagaState.COMPLETED
    assert log.get("o-1").steps_with("done") == [
        "create_order",
        "reserve_inventory",
        "charge_card",
        "schedule_shipment",
        "confirm_order",
    ]
    assert orders.status("o-1") == "CONFIRMED"
    assert (inventory.stock("sku-1"), payments.charged("o-1")) == (9, 120)


def test_pivot_failure_compensates_completed_steps_in_reverse_order() -> None:
    saga, log, orders, inventory, payments, _ = bench()
    assert saga.start("o-2", order("o-2", amount=900)) is SagaState.COMPENSATED
    record = log.get("o-2")
    assert record.steps_with("failed") == ["charge_card"]
    assert record.steps_with("compensated") == ["reserve_inventory", "create_order"]
    assert orders.status("o-2") == "CANCELLED"
    assert (inventory.stock("sku-1"), inventory.reservations(), payments.charged("o-2")) == (10, 0, 0)


def test_failure_before_the_pivot_only_undoes_what_ran() -> None:
    saga, log, orders, inventory, payments, _ = bench(stock=0)
    assert saga.start("o-3", order("o-3")) is SagaState.COMPENSATED
    record = log.get("o-3")
    assert record.steps_with("done") == ["create_order"]
    assert record.steps_with("compensated") == ["create_order"]
    assert orders.status("o-3") == "CANCELLED"
    assert payments.charged("o-3") == 0


def test_crash_before_the_done_record_is_resumed_and_the_step_reruns_idempotently() -> None:
    saga, log, orders, inventory, _, _ = bench()
    saga.crash_after("reserve_inventory")
    with pytest.raises(OrchestratorCrash):
        saga.start("o-4", order("o-4"))
    record = log.get("o-4")
    assert record.state is SagaState.RUNNING
    assert record.steps_with("done") == ["create_order"]
    assert record.steps_with("started") == ["create_order", "reserve_inventory"]
    assert saga.resume("o-4") is SagaState.COMPLETED
    assert record.steps_with("started").count("reserve_inventory") == 2  # re-run after resume
    assert (inventory.stock("sku-1"), inventory.reservations()) == (9, 1)  # not reserved twice
    assert orders.status("o-4") == "CONFIRMED"
    assert saga.resume("o-4") is SagaState.COMPLETED  # resuming a finished saga changes nothing
    assert record.steps_with("done").count("confirm_order") == 1


def test_transient_failures_are_retried_within_the_attempt_budget() -> None:
    saga, log, _, _, _, shipping = bench(max_attempts=3)
    shipping.fail_next(2)
    assert saga.start("o-5", order("o-5")) is SagaState.COMPLETED
    entries = [(e.step, e.event, e.attempt) for e in log.get("o-5").entries if e.step == "schedule_shipment"]
    assert entries == [
        ("schedule_shipment", "started", 1),
        ("schedule_shipment", "failed", 1),
        ("schedule_shipment", "failed", 2),
        ("schedule_shipment", "done", 1),
    ]


def test_retriable_step_that_never_succeeds_leaves_the_saga_stuck_not_rolled_back() -> None:
    saga, log, orders, inventory, payments, shipping = bench(max_attempts=3)
    shipping.fail_next(10)
    assert saga.start("o-6", order("o-6")) is SagaState.STUCK
    record = log.get("o-6")
    assert record.steps_with("failed") == ["schedule_shipment"] * 3
    assert record.steps_with("compensated") == []
    assert payments.charged("o-6") == 120  # past the pivot: the charge stays
    assert inventory.stock("sku-1") == 9
    assert orders.status("o-6") == "PENDING"


def test_failing_compensation_leaves_the_saga_stuck() -> None:
    def explode(_: SagaContext) -> None:
        raise TransientError("undo service down")

    def noop(_: SagaContext) -> None:
        return None

    def decline(_: SagaContext) -> None:
        raise TransientError("always times out")

    log = SagaLog()
    steps = [Step("a", noop, explode), Step("b", noop, noop), Step("pay", decline, kind=StepKind.PIVOT)]
    saga = SagaOrchestrator(steps, log, max_attempts=2)
    assert saga.start("s-1", {}) is SagaState.STUCK
    record = log.get("s-1")
    assert record.steps_with("compensated") == ["b"]  # b undone, a's undo failed twice
    assert [e for e in record.entries if e.step == "a" and e.event == "failed"][-1].attempt == 2


@pytest.mark.parametrize(
    ("steps", "max_attempts", "message"),
    [
        ([], 3, "at least one step"),
        ([Step("a", lambda c: None, lambda c: None)], 0, "max_attempts"),
        ([Step("a", lambda c: None, lambda c: None)] * 2, 3, "unique"),
        ([Step("a", lambda c: None)], 3, "needs a compensation"),
        ([Step("p", lambda c: None, kind=StepKind.PIVOT), Step("a", lambda c: None, lambda c: None)], 3, "after the pivot"),
        ([Step("p", lambda c: None, kind=StepKind.PIVOT), Step("q", lambda c: None, kind=StepKind.PIVOT)], 3, "at most one pivot"),
        ([Step("a", lambda c: None, lambda c: None), Step("r", lambda c: None, kind=StepKind.RETRIABLE)], 3, "follow the pivot"),
    ],
)
def test_saga_definitions_are_validated(steps: list[Step], max_attempts: int, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SagaOrchestrator(steps, SagaLog(), max_attempts=max_attempts)


def test_saga_ids_are_unique_and_unknown_sagas_cannot_be_resumed() -> None:
    saga, _, _, _, _, _ = bench()
    saga.start("o-7", order("o-7"))
    with pytest.raises(ConflictError):
        saga.start("o-7", order("o-7"))
    with pytest.raises(NotFoundError):
        saga.resume("o-missing")


def test_concurrent_sagas_keep_stock_and_charges_consistent() -> None:
    saga, log, _, inventory, payments, _ = bench(stock=50)

    def run(i: int) -> SagaState:
        amount = 900 if i % 3 == 0 else 100  # every third card is declined at the pivot
        return saga.start(f"o-{i}", order(f"o-{i}", amount=amount))

    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(run, range(60)))

    completed = states.count(SagaState.COMPLETED)
    assert completed == 40 and states.count(SagaState.COMPENSATED) == 20
    assert set(log.states().values()) == {SagaState.COMPLETED, SagaState.COMPENSATED}
    assert inventory.stock("sku-1") == 50 - completed
    assert inventory.reservations() == completed
    assert sum(payments.charged(f"o-{i}") for i in range(60)) == 100 * completed
