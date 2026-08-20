"""Money as integer minor units (cents). Never use floats for money."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True, slots=True, order=True)
class Money:
    cents: int
    currency: str = "USD"

    @classmethod
    def of(cls, amount: str | int | float | Decimal, currency: str = "USD") -> Money:
        """Build from a human amount such as ``"12.34"`` (rounds half-up to cents)."""
        quantized = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return cls(int(quantized * 100), currency)

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.cents + other.cents, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.cents - other.cents, self.currency)

    def __mul__(self, factor: int | Decimal) -> Money:
        value = (Decimal(self.cents) * Decimal(factor)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return Money(int(value), self.currency)

    def __neg__(self) -> Money:
        return Money(-self.cents, self.currency)

    def is_zero(self) -> bool:
        return self.cents == 0

    def allocate(self, ratios: list[int]) -> list[Money]:
        """Split without losing cents: remainder goes to the first shares (deterministic).

        >>> [m.cents for m in Money(100).allocate([1, 1, 1])]
        [34, 33, 33]
        """
        if not ratios or any(r < 0 for r in ratios) or sum(ratios) == 0:
            raise ValueError("ratios must be non-empty, non-negative and not all zero")
        total = sum(ratios)
        shares = [self.cents * r // total for r in ratios]
        remainder = self.cents - sum(shares)
        for i in range(abs(remainder)):
            shares[i % len(shares)] += 1 if remainder > 0 else -1
        return [Money(s, self.currency) for s in shares]

    def __str__(self) -> str:
        sign = "-" if self.cents < 0 else ""
        return f"{sign}{abs(self.cents) // 100}.{abs(self.cents) % 100:02d} {self.currency}"
