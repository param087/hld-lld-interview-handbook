"""Change-making policies (Strategy).

Both take a *snapshot* of the payable coins rather than the cash box itself, so
they are pure functions: no lock, no mutation, trivial to test and to compare.
"""

from __future__ import annotations

from typing import Protocol

from common import Money
from lld.vending_machine.models import Coin, InsufficientChangeError


# --8<-- [start:change]
class ChangeMaker(Protocol):
    """Plans the coins to pay out. Raises when the box cannot make the amount."""

    name: str

    def plan(self, amount: Money, available: dict[Coin, int]) -> tuple[Coin, ...]: ...


class GreedyChangeMaker:
    """Largest coin that still fits, over and over. Fast, and incomplete.

    With unlimited coins greedy is optimal for this denomination set. With a real
    cash box it is not even *correct*: 30 cents from one quarter and three dimes
    fails, because greedy commits to the quarter and then has no five-cent coin.
    """

    name = "greedy"

    def plan(self, amount: Money, available: dict[Coin, int]) -> tuple[Coin, ...]:
        remaining = amount.cents
        chosen: list[Coin] = []
        for coin in sorted(available, reverse=True):
            take = min(available[coin], remaining // int(coin))
            chosen.extend([coin] * take)
            remaining -= int(coin) * take
        if remaining:
            raise InsufficientChangeError(f"cannot make {amount} from the coins in the box")
        return tuple(chosen)


class MinimalChangeMaker:
    """Fewest coins that add up, searching every count of every denomination.

    Complete: it finds a combination whenever one exists. The search is memoised
    on (denomination index, remaining amount), so the number of distinct
    sub-problems is the number of denominations times the amount - a fixed
    ceiling that does not grow with how full the cash box is.
    """

    name = "minimal"

    def plan(self, amount: Money, available: dict[Coin, int]) -> tuple[Coin, ...]:
        denominations = sorted(available, reverse=True)
        memo: dict[tuple[int, int], tuple[Coin, ...] | None] = {}

        def solve(index: int, remaining: int) -> tuple[Coin, ...] | None:
            if remaining == 0:
                return ()
            if index == len(denominations):
                return None
            key = (index, remaining)
            if key in memo:
                return memo[key]
            coin = denominations[index]
            best: tuple[Coin, ...] | None = None
            for count in range(min(available[coin], remaining // int(coin)), -1, -1):
                rest = solve(index + 1, remaining - int(coin) * count)
                if rest is None:
                    continue
                candidate = (coin,) * count + rest
                if best is None or len(candidate) < len(best):
                    best = candidate
            memo[key] = best
            return best

        plan = solve(0, amount.cents)
        if plan is None:
            raise InsufficientChangeError(f"cannot make {amount} from the coins in the box")
        return plan


# --8<-- [end:change]
