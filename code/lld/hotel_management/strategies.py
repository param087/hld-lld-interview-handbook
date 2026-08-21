"""Seasonal pricing and cancellation policy: the two rules the interviewer will change."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from common import Money
from lld.hotel_management.models import DateRange, Reservation, RoomRequest, RoomType

# --8<-- [start:pricing]
DEFAULT_NIGHTLY_RATES: dict[RoomType, Money] = {
    RoomType.SINGLE: Money.of("80.00"),
    RoomType.DOUBLE: Money.of("120.00"),
    RoomType.DELUXE: Money.of("180.00"),
    RoomType.SUITE: Money.of("320.00"),
}

# High season: December to February and June to August in this hotel's market.
DEFAULT_SEASON_MULTIPLIERS: dict[int, Decimal] = {
    12: Decimal("1.4"),
    1: Decimal("1.4"),
    2: Decimal("1.2"),
    6: Decimal("1.3"),
    7: Decimal("1.5"),
    8: Decimal("1.3"),
}


class PricingStrategy(Protocol):
    """Price one room of one type for one night. Stateless, therefore thread-safe."""

    def price_night(self, room_type: RoomType, night: date) -> Money: ...


class SeasonalPricing:
    """Base rate per type, scaled by the month. The default policy."""

    def __init__(
        self,
        rates: dict[RoomType, Money] | None = None,
        multipliers: dict[int, Decimal] | None = None,
    ) -> None:
        self._rates = rates or DEFAULT_NIGHTLY_RATES
        self._multipliers = multipliers or DEFAULT_SEASON_MULTIPLIERS

    def price_night(self, room_type: RoomType, night: date) -> Money:
        return self._rates[room_type] * self._multipliers.get(night.month, Decimal("1.0"))


class FlatRatePricing:
    """One rate per type all year (corporate contracts, long stays)."""

    def __init__(self, rates: dict[RoomType, Money] | None = None) -> None:
        self._rates = rates or DEFAULT_NIGHTLY_RATES

    def price_night(self, room_type: RoomType, night: date) -> Money:
        return self._rates[room_type]


def quote_stay(
    pricing: PricingStrategy, rooms: tuple[RoomRequest, ...], stay: DateRange
) -> Money:
    """Sum every requested room over every night. Nightly pricing composes for free."""
    total = Money(0)
    for request in rooms:
        for night in stay.nights():
            total = total + pricing.price_night(request.room_type, night) * request.count
    return total


# --8<-- [end:pricing]


# --8<-- [start:cancellation]
class CancellationPolicy(Protocol):
    """How much of a confirmed reservation comes back when the guest cancels."""

    def refund(self, reservation: Reservation, today: date) -> Money: ...


class FreeUntilDaysBefore:
    """Free cancellation up to N days before arrival, then the first night is kept."""

    def __init__(self, days: int = 2) -> None:
        self._days = days

    def refund(self, reservation: Reservation, today: date) -> Money:
        stay = reservation.stay
        if (stay.start - today).days >= self._days:
            return reservation.amount
        if today >= stay.start:
            return Money(0, reservation.amount.currency)
        nights = stay.nights_count
        kept = reservation.amount.allocate([1] * nights)[0]  # one night, cents-exact
        return reservation.amount - kept


class NonRefundablePolicy:
    """Discounted advance-purchase rate: nothing comes back."""

    def refund(self, reservation: Reservation, today: date) -> Money:
        return Money(0, reservation.amount.currency)


# --8<-- [end:cancellation]
