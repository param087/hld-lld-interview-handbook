"""The four ways to split an expense, behind one interface (Strategy + Factory).

Every strategy returns one ``Split`` per participant and every strategy leans on
``Money.allocate``, which hands the leftover cents to the first shares instead
of dropping them. That is the whole reason money is integer cents here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from common import Money, ValidationError
from lld.splitwise.models import Split, SplitType, UnbalancedExpenseError

TOTAL_BASIS_POINTS = 10_000  # 100.00% expressed in hundredths of a percent


# --8<-- [start:strategy]
class SplitStrategy(Protocol):
    """Turns a total into one ``Split`` per participant. Stateless and thread-safe.

    ``weights`` means something different per strategy (cents, basis points,
    share units) and is ignored by the equal split. Keeping one signature is
    what lets ``ExpenseService`` stay free of ``if split_type == ...`` ladders.
    """

    def split(
        self, total: Money, participant_ids: Sequence[str], weights: Sequence[int] | None
    ) -> list[Split]: ...


def _check_participants(participant_ids: Sequence[str]) -> None:
    if not participant_ids:
        raise ValidationError("an expense needs at least one participant")
    if len(set(participant_ids)) != len(participant_ids):
        raise ValidationError("a participant may appear only once in a split")


def _check_weights(participant_ids: Sequence[str], weights: Sequence[int] | None) -> Sequence[int]:
    if weights is None:
        raise ValidationError("this split type needs one weight per participant")
    if len(weights) != len(participant_ids):
        raise ValidationError(f"expected {len(participant_ids)} weights, got {len(weights)}")
    if any(weight < 0 for weight in weights):
        raise ValidationError("weights cannot be negative")
    return weights


class EqualSplit:
    """Everyone pays the same, cents included: 100.01 over three is 33.34/33.34/33.33."""

    def split(
        self, total: Money, participant_ids: Sequence[str], weights: Sequence[int] | None
    ) -> list[Split]:
        _check_participants(participant_ids)
        shares = total.allocate([1] * len(participant_ids))
        return [Split(user_id, share) for user_id, share in zip(participant_ids, shares, strict=True)]


class ExactSplit:
    """The caller states every share in cents; the strategy only checks the sum."""

    def split(
        self, total: Money, participant_ids: Sequence[str], weights: Sequence[int] | None
    ) -> list[Split]:
        _check_participants(participant_ids)
        amounts = _check_weights(participant_ids, weights)
        if sum(amounts) != total.cents:
            stated = Money(sum(amounts), total.currency)
            raise UnbalancedExpenseError(f"exact shares add up to {stated}, expense is {total}")
        return [
            Split(user_id, Money(cents, total.currency))
            for user_id, cents in zip(participant_ids, amounts, strict=True)
        ]


class PercentSplit:
    """Percentages arrive as basis points, so 33.33% is the integer 3333, never a float."""

    def split(
        self, total: Money, participant_ids: Sequence[str], weights: Sequence[int] | None
    ) -> list[Split]:
        _check_participants(participant_ids)
        points = _check_weights(participant_ids, weights)
        if sum(points) != TOTAL_BASIS_POINTS:
            raise UnbalancedExpenseError(
                f"percentages add up to {sum(points) / 100:.2f}%, they must add up to 100%"
            )
        shares = total.allocate(list(points))
        return [Split(user_id, share) for user_id, share in zip(participant_ids, shares, strict=True)]


class ShareSplit:
    """Relative shares: a couple counts as 2, a single person as 1, no percentages needed."""

    def split(
        self, total: Money, participant_ids: Sequence[str], weights: Sequence[int] | None
    ) -> list[Split]:
        _check_participants(participant_ids)
        units = _check_weights(participant_ids, weights)
        if sum(units) == 0:
            raise ValidationError("share weights cannot all be zero")
        shares = total.allocate(list(units))
        return [Split(user_id, share) for user_id, share in zip(participant_ids, shares, strict=True)]


class SplitStrategyFactory:
    """Factory: the API receives a ``SplitType`` string, the registry maps it to a strategy."""

    _registry: dict[SplitType, type] = {
        SplitType.EQUAL: EqualSplit,
        SplitType.EXACT: ExactSplit,
        SplitType.PERCENT: PercentSplit,
        SplitType.SHARE: ShareSplit,
    }

    @classmethod
    def create(cls, split_type: SplitType | str) -> SplitStrategy:
        try:
            strategy_class = cls._registry[SplitType(split_type)]
        except ValueError as exc:
            raise ValidationError(f"unknown split type: {split_type!r}") from exc
        return strategy_class()


# --8<-- [end:strategy]
