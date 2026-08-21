"""Saga orchestrator with compensations, a pivot step, retriable steps and a saga log.

What the module demonstrates, in the order an interviewer asks about it:

* ``SagaOrchestrator.start`` runs the steps of a saga in order and writes every transition to
  a ``SagaLog`` before acting on it, so ``resume`` can continue a saga whose orchestrator died
  between two log writes. The step that was in flight is simply run again, which is why every
  step must be idempotent.
* Steps come in three kinds. A *compensatable* step has an undo action. The *pivot* is the
  go/no-go step, the last one allowed to fail for a business reason (card declined). The
  *retriable* steps after it are retried until they succeed and must never fail for good.
* A failure at or before the pivot runs the compensations of every completed step in reverse
  order. A retriable step that exhausts its attempts leaves the saga ``STUCK`` for an operator:
  the money has moved and no compensation can take it back.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from common import ConflictError, HandbookError, NotFoundError, ValidationError

SagaContext = dict[str, Any]
StepAction = Callable[[SagaContext], None]


# --8<-- [start:steps]
class StepKind(StrEnum):
    COMPENSATABLE = "compensatable"  # may fail; undone by its compensation
    PIVOT = "pivot"  # the go/no-go step: the last one that may fail
    RETRIABLE = "retriable"  # after the pivot: retried until it succeeds


class SagaState(StrEnum):
    RUNNING = "running"
    COMPENSATING = "compensating"
    COMPLETED = "completed"
    COMPENSATED = "compensated"
    STUCK = "stuck"  # a retriable step or a compensation exhausted its attempts


class StepFailed(HandbookError):
    """A business failure inside a step (card declined, out of stock): do not retry."""


class TransientError(HandbookError):
    """A timeout or a 5xx from a downstream service: retry the same attempt."""


class OrchestratorCrash(HandbookError):
    """Failure injection: the orchestrator process dies between two log writes."""


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    action: StepAction
    compensation: StepAction | None = None
    kind: StepKind = StepKind.COMPENSATABLE


@dataclass(frozen=True, slots=True)
class LogEntry:
    step: str
    event: str  # started, done, failed, compensated
    attempt: int = 1
    detail: str = ""


@dataclass(slots=True)
class SagaRecord:
    saga_id: str
    context: SagaContext
    state: SagaState = SagaState.RUNNING
    entries: list[LogEntry] = field(default_factory=list)

    def steps_with(self, event: str) -> list[str]:
        return [entry.step for entry in self.entries if entry.event == event]


# --8<-- [end:steps]


# --8<-- [start:log]
class SagaLog:
    """The orchestrator's durable state: one record per saga.

    ``_lock`` guards the record table and every append or state change. A record is only
    ever driven by one orchestrator thread at a time, so its reads need no lock.
    """

    def __init__(self) -> None:
        self._records: dict[str, SagaRecord] = {}
        self._lock = threading.Lock()

    def create(self, saga_id: str, context: SagaContext) -> SagaRecord:
        with self._lock:
            if saga_id in self._records:
                raise ConflictError(f"saga {saga_id!r} already exists")
            record = SagaRecord(saga_id, dict(context))
            self._records[saga_id] = record
            return record

    def get(self, saga_id: str) -> SagaRecord:
        with self._lock:
            if saga_id not in self._records:
                raise NotFoundError(f"saga {saga_id!r} not found")
            return self._records[saga_id]

    def append(self, record: SagaRecord, entry: LogEntry) -> None:
        with self._lock:
            record.entries.append(entry)

    def set_state(self, record: SagaRecord, state: SagaState) -> None:
        with self._lock:
            record.state = state

    def states(self) -> dict[str, SagaState]:
        with self._lock:
            return {saga_id: record.state for saga_id, record in self._records.items()}


# --8<-- [end:log]


# --8<-- [start:orchestrator]
class SagaOrchestrator:
    """Drives one saga definition. ``_lock`` guards the crash-injection flag only."""

    def __init__(self, steps: Sequence[Step], log: SagaLog, max_attempts: int = 3) -> None:
        if not steps:
            raise ValidationError("a saga needs at least one step")
        if max_attempts < 1:
            raise ValidationError("max_attempts must be at least 1")
        self._steps = tuple(steps)
        self._log = log
        self._max_attempts = max_attempts
        self._crash_after: str | None = None
        self._lock = threading.Lock()
        self._validate()

    def _validate(self) -> None:
        names = [step.name for step in self._steps]
        if len(set(names)) != len(names):
            raise ValidationError("step names must be unique")
        has_compensatable = any(s.kind is StepKind.COMPENSATABLE for s in self._steps)
        seen_pivot = False
        for step in self._steps:
            if step.kind is StepKind.COMPENSATABLE:
                if seen_pivot:
                    raise ValidationError(f"{step.name}: no compensatable step after the pivot")
                if step.compensation is None:
                    raise ValidationError(f"{step.name}: a compensatable step needs a compensation")
            elif step.kind is StepKind.PIVOT:
                if seen_pivot:
                    raise ValidationError("a saga has at most one pivot")
                seen_pivot = True
            elif has_compensatable and not seen_pivot:
                raise ValidationError(f"{step.name}: retriable steps must follow the pivot")

    def crash_after(self, step_name: str | None) -> None:
        """Arm a crash right after ``step_name`` runs, before its 'done' record is written."""
        with self._lock:
            self._crash_after = step_name

    def start(self, saga_id: str, context: SagaContext) -> SagaState:
        return self._drive(self._log.create(saga_id, context))

    def resume(self, saga_id: str) -> SagaState:
        """Continue after a crash: a step that was in flight is run again (it must be idempotent)."""
        return self._drive(self._log.get(saga_id))

    def _drive(self, record: SagaRecord) -> SagaState:
        if record.state is SagaState.RUNNING:
            done = set(record.steps_with("done"))
            for step in self._steps:
                if step.name in done:
                    continue
                if self._run_forward(record, step):
                    continue
                stuck = step.kind is StepKind.RETRIABLE  # past the pivot: nothing to undo
                self._log.set_state(record, SagaState.STUCK if stuck else SagaState.COMPENSATING)
                break
            else:
                self._log.set_state(record, SagaState.COMPLETED)
        if record.state is SagaState.COMPENSATING:
            self._compensate(record)
        return record.state

    def _run_forward(self, record: SagaRecord, step: Step) -> bool:
        self._log.append(record, LogEntry(step.name, "started"))
        if not self._attempt(record, step.name, step.action):
            return False
        self._maybe_crash(step.name)
        self._log.append(record, LogEntry(step.name, "done"))
        return True

    def _compensate(self, record: SagaRecord) -> None:
        done = set(record.steps_with("done"))
        undone = set(record.steps_with("compensated"))
        for step in reversed(self._steps):
            if step.name not in done or step.name in undone or step.compensation is None:
                continue
            if not self._attempt(record, step.name, step.compensation):
                self._log.set_state(record, SagaState.STUCK)
                return
            self._log.append(record, LogEntry(step.name, "compensated"))
        self._log.set_state(record, SagaState.COMPENSATED)

    def _attempt(self, record: SagaRecord, name: str, action: StepAction) -> bool:
        for attempt in range(1, self._max_attempts + 1):
            try:
                action(record.context)
                return True
            except StepFailed as exc:
                self._log.append(record, LogEntry(name, "failed", attempt, str(exc)))
                return False
            except TransientError as exc:
                self._log.append(record, LogEntry(name, "failed", attempt, str(exc)))
        return False

    def _maybe_crash(self, step_name: str) -> None:
        with self._lock:
            if self._crash_after != step_name:
                return
            self._crash_after = None
        raise OrchestratorCrash(f"orchestrator crashed after {step_name} ran, before its done record")


# --8<-- [end:orchestrator]


# --8<-- [start:order_saga]
class OrderService:
    def __init__(self) -> None:
        self._status: dict[str, str] = {}
        self._lock = threading.Lock()

    def set_status(self, order_id: str, status: str) -> None:
        with self._lock:
            self._status[order_id] = status

    def status(self, order_id: str) -> str:
        with self._lock:
            return self._status.get(order_id, "NONE")


class InventoryService:
    """Reservations keyed by order id make reserve and release idempotent: a repeat is a no-op."""

    def __init__(self, stock: Mapping[str, int]) -> None:
        self._stock = dict(stock)
        self._reservations: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def reserve(self, order_id: str, sku: str, qty: int) -> None:
        with self._lock:
            if order_id in self._reservations:
                return
            if self._stock.get(sku, 0) < qty:
                raise StepFailed(f"{sku}: {self._stock.get(sku, 0)} left, {qty} wanted")
            self._stock[sku] -= qty
            self._reservations[order_id] = (sku, qty)

    def release(self, order_id: str) -> None:
        with self._lock:
            reservation = self._reservations.pop(order_id, None)
            if reservation is not None:
                self._stock[reservation[0]] += reservation[1]

    def stock(self, sku: str) -> int:
        with self._lock:
            return self._stock.get(sku, 0)

    def reservations(self) -> int:
        with self._lock:
            return len(self._reservations)


class PaymentService:
    def __init__(self, credit_limit: int) -> None:
        self._limit = credit_limit
        self._charged: dict[str, int] = {}
        self._lock = threading.Lock()

    def charge(self, order_id: str, amount: int) -> None:
        with self._lock:
            if order_id in self._charged:
                return
            if amount > self._limit:
                raise StepFailed(f"card declined: {amount} over the {self._limit} limit")
            self._charged[order_id] = amount

    def charged(self, order_id: str) -> int:
        with self._lock:
            return self._charged.get(order_id, 0)


class ShippingService:
    """Times out ``fail_next`` times before it answers: the test bench for retriable steps."""

    def __init__(self) -> None:
        self._failures_left = 0
        self._scheduled: set[str] = set()
        self._lock = threading.Lock()

    def fail_next(self, count: int) -> None:
        with self._lock:
            self._failures_left = count

    def schedule(self, order_id: str) -> None:
        with self._lock:
            if order_id in self._scheduled:
                return
            if self._failures_left > 0:
                self._failures_left -= 1
                raise TransientError("shipping service timed out")
            self._scheduled.add(order_id)


def order_saga(
    orders: OrderService,
    inventory: InventoryService,
    payments: PaymentService,
    shipping: ShippingService,
    log: SagaLog,
    max_attempts: int = 3,
) -> SagaOrchestrator:
    """Create order, reserve stock, charge the card (pivot), then ship and confirm (retriable)."""

    def set_status(value: str) -> StepAction:
        return lambda ctx: orders.set_status(ctx["order_id"], value)

    steps = [
        Step("create_order", set_status("PENDING"), set_status("CANCELLED")),
        Step(
            "reserve_inventory",
            lambda ctx: inventory.reserve(ctx["order_id"], ctx["sku"], ctx["qty"]),
            lambda ctx: inventory.release(ctx["order_id"]),
        ),
        Step("charge_card", lambda ctx: payments.charge(ctx["order_id"], ctx["amount"]), kind=StepKind.PIVOT),
        Step("schedule_shipment", lambda ctx: shipping.schedule(ctx["order_id"]), kind=StepKind.RETRIABLE),
        Step("confirm_order", set_status("CONFIRMED"), kind=StepKind.RETRIABLE),
    ]
    return SagaOrchestrator(steps, log, max_attempts=max_attempts)


# --8<-- [end:order_saga]


def trail(record: SagaRecord) -> str:
    """One token per log entry, 'started' omitted: create_order=done charge_card=failed ..."""
    return " ".join(f"{e.step}={e.event}" for e in record.entries if e.event != "started")


def main() -> None:
    orders, inventory = OrderService(), InventoryService({"sku-1": 10})
    payments, shipping, log = PaymentService(credit_limit=500), ShippingService(), SagaLog()
    saga = order_saga(orders, inventory, payments, shipping, log)

    def order(order_id: str, amount: int) -> SagaContext:
        return {"order_id": order_id, "sku": "sku-1", "qty": 1, "amount": amount}

    def show(label: str, saga_id: str, state: SagaState) -> None:
        print(
            f"{label:<28} -> {state.value}; order={orders.status(saga_id)} "
            f"stock={inventory.stock('sku-1')}; {trail(log.get(saga_id))}"
        )

    show("o-1 happy path", "o-1", saga.start("o-1", order("o-1", 120)))
    show("o-2 card declined at pivot", "o-2", saga.start("o-2", order("o-2", 900)))

    saga.crash_after("reserve_inventory")
    try:
        saga.start("o-3", order("o-3", 120))
    except OrchestratorCrash as exc:
        print(f"o-3 {exc}")
    entries = [f"{e.step}:{e.event}" for e in log.get("o-3").entries]
    print(f"    log before resume: {entries}; reservations={inventory.reservations()}")
    state = saga.resume("o-3")
    print(
        f"    resume re-runs reserve_inventory, still {inventory.reservations()} reservations "
        f"(idempotent) -> {state.value}; order={orders.status('o-3')} stock={inventory.stock('sku-1')}"
    )

    shipping.fail_next(2)
    show("o-4 shipping times out twice", "o-4", saga.start("o-4", order("o-4", 120)))

    shipping.fail_next(10)
    show("o-5 shipping down for good", "o-5", saga.start("o-5", order("o-5", 120)))
    print(f"    card charged {payments.charged('o-5')} and kept: past the pivot nothing is undone; page a human")


if __name__ == "__main__":
    main()
