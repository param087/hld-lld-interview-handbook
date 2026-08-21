"""Fine calculation: the one rule every library board changes every year."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from common import Money
from lld.library_management.models import Loan

# --8<-- [start:fines]
DEFAULT_DAILY_RATE = Money.of("0.25")
DEFAULT_CAP = Money.of("10.00")
DEFAULT_LOST_ITEM_FEE = Money.of("30.00")
TIER_ONE_RATE = Money.of("0.10")
TIER_TWO_RATE = Money.of("0.50")


class FinePolicy(Protocol):
    """Turn an overdue loan into money. Stateless, therefore thread-safe."""

    def fine_for(self, loan: Loan, today: date) -> Money: ...


class PerDayFine:
    """A rate per day late, a grace period, and a cap so a lost book is not infinite."""

    def __init__(
        self,
        daily_rate: Money = DEFAULT_DAILY_RATE,
        grace_days: int = 0,
        cap: Money = DEFAULT_CAP,
    ) -> None:
        self._rate = daily_rate
        self._grace = grace_days
        self._cap = cap

    def fine_for(self, loan: Loan, today: date) -> Money:
        late = loan.days_overdue(today) - self._grace
        if late <= 0:
            return Money(0)
        return min(self._rate * late, self._cap)


class TieredFine:
    """Cheap for the first week, painful after it - what most public libraries do."""

    def __init__(
        self,
        first_week: Money = TIER_ONE_RATE,
        after: Money = TIER_TWO_RATE,
        cap: Money = DEFAULT_CAP,
    ) -> None:
        self._first_week = first_week
        self._after = after
        self._cap = cap

    def fine_for(self, loan: Loan, today: date) -> Money:
        late = loan.days_overdue(today)
        if late <= 0:
            return Money(0)
        cheap_days = min(late, 7)
        total = self._first_week * cheap_days + self._after * max(0, late - 7)
        return min(total, self._cap)


class NoFine:
    """Amnesty week, or a children's library that never fines."""

    def fine_for(self, loan: Loan, today: date) -> Money:
        return Money(0)


# --8<-- [end:fines]
