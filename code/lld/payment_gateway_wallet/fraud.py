"""Fraud and limit rules as a Chain of Responsibility.

Each link either has an opinion (block, with a reason) or passes the request on.
The first rule with an opinion wins; a request that reaches the end of the chain
is allowed. Ordering is a policy decision -- cheap checks first, then the ones
that have to scan history.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from common import Money
from lld.payment_gateway_wallet.models import Transaction, TransactionStatus


# --8<-- [start:fraud]
@dataclass(frozen=True, slots=True)
class FraudContext:
    """Everything a rule may look at. Built once per payment, never mutated."""

    wallet_id: str
    amount: Money
    destination: str
    at: float
    history: tuple[Transaction, ...]  # this wallet's transactions, newest last


@dataclass(frozen=True, slots=True)
class FraudDecision:
    allowed: bool
    rule: str = ""
    reason: str = ""

    @classmethod
    def allow(cls) -> FraudDecision:
        return cls(True)

    @classmethod
    def block(cls, rule: str, reason: str) -> FraudDecision:
        return cls(False, rule, reason)


class FraudRule(ABC):
    """One link. ``check`` returns a decision to stop on, or None to pass along."""

    def __init__(self) -> None:
        self._next: FraudRule | None = None

    def set_next(self, rule: FraudRule) -> FraudRule:
        self._next = rule
        return rule  # so a chain reads a.set_next(b).set_next(c)

    @abstractmethod
    def check(self, context: FraudContext) -> FraudDecision | None: ...

    def evaluate(self, context: FraudContext) -> FraudDecision:
        decision = self.check(context)
        if decision is not None:
            return decision
        if self._next is None:
            return FraudDecision.allow()
        return self._next.evaluate(context)


class AmountCeilingRule(FraudRule):
    """The cheapest check: one transaction may not exceed a hard ceiling."""

    def __init__(self, ceiling: Money) -> None:
        super().__init__()
        self._ceiling = ceiling

    def check(self, context: FraudContext) -> FraudDecision | None:
        if context.amount > self._ceiling:
            return FraudDecision.block("amount_ceiling", f"{context.amount} exceeds the {self._ceiling} ceiling")
        return None


class DenylistRule(FraudRule):
    """Destinations the platform refuses outright, whatever the amount."""

    def __init__(self, denied: Sequence[str]) -> None:
        super().__init__()
        self._denied = frozenset(denied)

    def check(self, context: FraudContext) -> FraudDecision | None:
        if context.destination in self._denied:
            return FraudDecision.block("denylist", f"{context.destination} is on the denylist")
        return None


class VelocityRule(FraudRule):
    """Too many payments in a short window is the classic stolen-credentials signal."""

    def __init__(self, max_count: int, window_seconds: float) -> None:
        super().__init__()
        self._max_count = max_count
        self._window = window_seconds

    def check(self, context: FraudContext) -> FraudDecision | None:
        recent = [t for t in context.history if context.at - t.created_at <= self._window]
        if len(recent) >= self._max_count:
            return FraudDecision.block(
                "velocity", f"{len(recent)} payments in the last {int(self._window)}s"
            )
        return None


class DailyLimitRule(FraudRule):
    """The limit policy: the sum of today's settled outgoings plus this one has a cap."""

    def __init__(self, daily_cap: Money, day_seconds: float = 86_400) -> None:
        super().__init__()
        self._cap = daily_cap
        self._day = day_seconds

    def check(self, context: FraudContext) -> FraudDecision | None:
        spent = Money(0, context.amount.currency)
        for transaction in context.history:
            settled = transaction.status in (TransactionStatus.CAPTURED, TransactionStatus.AUTHORIZED)
            if settled and context.at - transaction.created_at <= self._day:
                spent = spent + transaction.amount
        if spent + context.amount > self._cap:
            return FraudDecision.block("daily_limit", f"{spent} already spent today, cap is {self._cap}")
        return None


def build_default_chain(
    ceiling: Money, daily_cap: Money, denied: Sequence[str] = (), max_per_minute: int = 5
) -> FraudRule:
    """Cheap checks first, history scans last."""
    head = AmountCeilingRule(ceiling)
    head.set_next(DenylistRule(denied)).set_next(VelocityRule(max_per_minute, 60.0)).set_next(
        DailyLimitRule(daily_cap)
    )
    return head


# --8<-- [end:fraud]
