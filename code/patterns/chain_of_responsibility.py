"""Chain of Responsibility: pass a request along a line of handlers.

The running example is an ATM cash dispenser. ``CashDispenser`` (the client)
links one ``DenominationHandler`` per note slot into a chain, largest note
first. Each handler takes the notes it can contribute and passes the remainder
to the next link; the dispenser commits the plan only once the chain has
covered the whole amount, so a failed withdrawal never touches the inventory.

The second half shows the two Pythonic forms: a list of callables where the
first rule with an opinion wins (the pure, first-handler-wins chain, here fraud
rules on a payment), and a pipeline of generator stages where every link keeps,
drops or rewrites what flows through and hands the rest on (the shape of
logging propagation and middleware).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise

from common import ConflictError, Money, ValidationError


class CannotDispenseError(ConflictError):
    """The slots cannot cover the amount with the notes they currently hold."""


# --8<-- [start:chain]
@dataclass(slots=True)
class CashRequest:
    """The request that travels down the chain: what is still owed, and the plan so far."""

    amount: int
    remaining: int
    notes: dict[int, int] = field(default_factory=dict)


class Handler(ABC):
    """Abstract handler: owns the link to the next handler and knows how to pass a request on.

    Every concrete handler must decide what to do (``handle`` is abstract); the base
    class only supplies ``forward``. A handler that does not call ``forward`` ends the
    chain, on purpose and visibly. ``set_next`` returns the handler it was given so a
    chain reads ``a.set_next(b).set_next(c)``.
    """

    def __init__(self) -> None:
        self._next: Handler | None = None

    def set_next(self, handler: Handler) -> Handler:
        self._next = handler
        return handler

    @abstractmethod
    def handle(self, request: CashRequest) -> CashRequest: ...

    def forward(self, request: CashRequest) -> CashRequest:
        if self._next is None:
            return request
        return self._next.handle(request)


class DenominationHandler(Handler):
    """One note slot. It takes the notes it can contribute and forwards the remainder.

    ``handle`` plans and never mutates ``count``: the chain decides *whether* the
    amount can be covered, the dispenser decides *when* to commit (``dispense``).
    """

    def __init__(self, denomination: int, count: int) -> None:
        super().__init__()
        if denomination <= 0 or count < 0:
            raise ValidationError("a slot needs a positive denomination and a non-negative count")
        self.denomination = denomination
        self.count = count

    def handle(self, request: CashRequest) -> CashRequest:
        notes = min(self.count, request.remaining // self.denomination)
        if notes:
            request.notes[self.denomination] = notes
            request.remaining -= notes * self.denomination
        return self.forward(request)

    def dispense(self, notes: int) -> None:
        if notes > self.count:
            raise CannotDispenseError(f"slot {self.denomination} holds {self.count}, asked for {notes}")
        self.count -= notes


# --8<-- [end:chain]


# --8<-- [start:dispenser]
class CashDispenser:
    """The client: builds the chain once, validates, plans, then commits under one lock.

    Two withdrawals planning against the same slots at the same time could both see
    enough notes, so ``_lock`` serialises plan-and-commit; a slot count never goes
    negative and a failed plan leaves every slot untouched.
    """

    def __init__(self, slots: Iterable[DenominationHandler]) -> None:
        self._slots = sorted(slots, key=lambda slot: slot.denomination, reverse=True)
        denominations = [slot.denomination for slot in self._slots]
        if not denominations or len(set(denominations)) != len(denominations):
            raise ValidationError("a dispenser needs at least one slot and one slot per denomination")
        for upper, lower in pairwise(self._slots):
            upper.set_next(lower)
        self._head: Handler = self._slots[0]
        self._lock = threading.Lock()

    @property
    def inventory(self) -> dict[int, int]:
        return {slot.denomination: slot.count for slot in self._slots}

    def withdraw(self, amount: int) -> dict[int, int]:
        smallest = self._slots[-1].denomination
        if amount <= 0 or amount % smallest:
            raise ValidationError(f"amount must be a positive multiple of {smallest}")
        with self._lock:
            plan = self._head.handle(CashRequest(amount=amount, remaining=amount))
            if plan.remaining:
                raise CannotDispenseError(f"cannot dispense {amount}: short by {plan.remaining}")
            for slot in self._slots:
                slot.dispense(plan.notes.get(slot.denomination, 0))
        return plan.notes


# --8<-- [end:dispenser]


# --8<-- [start:functional]
@dataclass(frozen=True, slots=True)
class Payment:
    amount: Money
    country: str
    attempts_last_hour: int


# A link as a function: return a rejection reason, or None to pass the payment on.
type FraudRule = Callable[[Payment], str | None]


def denied_country(denied: frozenset[str]) -> FraudRule:
    return lambda payment: f"country {payment.country} is denied" if payment.country in denied else None


def over_limit(limit: Money) -> FraudRule:
    return lambda payment: f"{payment.amount} exceeds {limit}" if payment.amount > limit else None


def too_many_attempts(max_attempts: int) -> FraudRule:
    def rule(payment: Payment) -> str | None:
        if payment.attempts_last_hour > max_attempts:
            return f"{payment.attempts_last_hour} attempts in the last hour"
        return None

    return rule


def first_rejection(rules: Sequence[FraudRule], payment: Payment) -> str | None:
    """The pure chain: the first rule with an opinion wins; None means every link passed."""
    for rule in rules:
        if (reason := rule(payment)) is not None:
            return reason
    return None


# --8<-- [end:functional]


# --8<-- [start:pipeline]
@dataclass(frozen=True, slots=True)
class LogRecord:
    level: int
    logger: str
    message: str


# A link as a generator stage: keep, drop or rewrite records, then hand the rest on.
type Stage = Callable[[Iterable[LogRecord]], Iterator[LogRecord]]


def at_least(level: int) -> Stage:
    def stage(records: Iterable[LogRecord]) -> Iterator[LogRecord]:
        return (record for record in records if record.level >= level)

    return stage


def redact(secret: str) -> Stage:
    def stage(records: Iterable[LogRecord]) -> Iterator[LogRecord]:
        for record in records:
            yield replace(record, message=record.message.replace(secret, "***"))

    return stage


def run_stages(stages: Sequence[Stage], records: Iterable[LogRecord]) -> Iterator[LogRecord]:
    """Every stage sees what the previous one let through; nothing runs until you iterate."""
    for stage in stages:
        records = stage(records)
    return iter(records)


# --8<-- [end:pipeline]


def _describe(plan: dict[int, int]) -> str:
    return ", ".join(f"{denomination} x {notes}" for denomination, notes in plan.items())


def main() -> None:
    dispenser = CashDispenser(
        [DenominationHandler(100, 10), DenominationHandler(2000, 2), DenominationHandler(500, 5)]
    )
    print(f"slots (largest first): {_describe(dispenser.inventory)}")
    for amount in (3700, 4600, 2500):
        try:
            plan = dispenser.withdraw(amount)
            print(f"withdraw {amount}: {_describe(plan)}")
        except CannotDispenseError as exc:
            print(f"withdraw {amount}: {exc}")
        print(f"  slots now: {_describe(dispenser.inventory)}")
    try:
        dispenser.withdraw(250)
    except ValidationError as exc:
        print(f"rejected before the chain ran: {exc}")

    print("--- pure chain: the first fraud rule with an opinion wins ---")
    rules: list[FraudRule] = [
        denied_country(frozenset({"KP", "IR"})),
        over_limit(Money.of("1000.00")),
        too_many_attempts(3),
    ]
    payments = [
        Payment(Money.of("80.00"), "US", 1),
        Payment(Money.of("2500.00"), "US", 1),
        Payment(Money.of("40.00"), "KP", 1),
        Payment(Money.of("40.00"), "DE", 7),
    ]
    for payment in payments:
        verdict = first_rejection(rules, payment) or "approved"
        print(f"{payment.country} {payment.amount} x{payment.attempts_last_hour}: {verdict}")

    print("--- generator pipeline: each stage keeps what it handles and hands the rest on ---")
    records = [
        LogRecord(10, "app.db", "connect token=abc123"),
        LogRecord(20, "app.http", "GET /orders 200"),
        LogRecord(40, "app.db", "timeout token=abc123"),
    ]
    for record in run_stages([at_least(20), redact("abc123")], records):
        print(f"{record.level:>2} {record.logger}: {record.message}")


if __name__ == "__main__":
    main()
