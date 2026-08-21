"""The two policies the interviewer will ask you to change: pricing and refunds."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from common import Money
from lld.movie_ticket_booking.models import Booking, SeatType, Show, ShowSeat

# --8<-- [start:pricing]
DEFAULT_SEAT_MULTIPLIERS: dict[SeatType, Decimal] = {
    SeatType.REGULAR: Decimal("1.0"),
    SeatType.PREMIUM: Decimal("1.5"),
    SeatType.RECLINER: Decimal("2.5"),
}


class PricingStrategy(Protocol):
    """Price one seat of one show. Stateless, therefore thread-safe."""

    def price(self, show: Show, show_seat: ShowSeat) -> Money: ...


class SeatTypePricing:
    """The show's base price scaled by the seat type. The default policy."""

    def __init__(self, multipliers: dict[SeatType, Decimal] | None = None) -> None:
        self._multipliers = multipliers or DEFAULT_SEAT_MULTIPLIERS

    def price(self, show: Show, show_seat: ShowSeat) -> Money:
        return show.base_price * self._multipliers[show_seat.seat.type]


class WeekendSurgePricing:
    """Composes any other strategy and adds a weekend surcharge.

    Composition rather than subclassing: a surge on top of a loyalty price is one
    more wrapper, not a new class in a combinatorial hierarchy.
    """

    def __init__(self, inner: PricingStrategy, surge: Decimal = Decimal("1.2")) -> None:
        self._inner = inner
        self._surge = surge

    def price(self, show: Show, show_seat: ShowSeat) -> Money:
        base = self._inner.price(show, show_seat)
        weekday = datetime.fromtimestamp(show.starts_at, tz=UTC).weekday()
        return base * self._surge if weekday >= 5 else base


# --8<-- [end:pricing]


# --8<-- [start:refunds]
class RefundPolicy(Protocol):
    """How much of a confirmed booking comes back when the user cancels."""

    def refund(self, booking: Booking, show: Show, now: float) -> Money: ...


class TieredRefundPolicy:
    """Full refund early, half inside the cut-off, nothing once the show has started."""

    def __init__(self, full_refund_before_seconds: int = 4 * 3600) -> None:
        self._cutoff = full_refund_before_seconds

    def refund(self, booking: Booking, show: Show, now: float) -> Money:
        seconds_to_show = show.starts_at - now
        if seconds_to_show <= 0:
            return Money(0, booking.amount.currency)
        if seconds_to_show >= self._cutoff:
            return booking.amount
        return booking.amount.allocate([1, 1])[0]  # half, cents-exact


class NoRefundPolicy:
    """Non-refundable inventory (previews, festival shows)."""

    def refund(self, booking: Booking, show: Show, now: float) -> Money:
        return Money(0, booking.amount.currency)


# --8<-- [end:refunds]
