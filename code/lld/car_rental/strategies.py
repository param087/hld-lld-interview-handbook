"""Pluggable policies: rate plans (Strategy), add-ons (Decorator), charges, payments."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from decimal import Decimal
from typing import Protocol

from common import Money, ValidationError
from lld.car_rental.models import (
    AddOnType,
    InvoiceLine,
    PaymentMethod,
    Reservation,
    VehicleType,
    total_of,
)


class PaymentProcessor(Protocol):
    """The card terminal at the desk. Injected so tests can decline a charge."""

    def charge(self, amount: Money, method: PaymentMethod) -> bool: ...


class AlwaysApprovesProcessor:
    def charge(self, amount: Money, method: PaymentMethod) -> bool:
        return True



# --8<-- [start:rates]
DEFAULT_DAILY_RATES: dict[VehicleType, Money] = {
    VehicleType.ECONOMY: Money.of("35.00"),
    VehicleType.SEDAN: Money.of("45.00"),
    VehicleType.SUV: Money.of("65.00"),
    VehicleType.VAN: Money.of("80.00"),
    VehicleType.LUXURY: Money.of("120.00"),
}


class RatePlan(Protocol):
    """Turns a class and a number of days into itemised invoice lines.

    Lines rather than a single total: the customer sees why they pay, and the
    insurance add-on can price itself as a percentage of everything above it.
    """

    def price(self, vehicle_type: VehicleType, days: int) -> tuple[InvoiceLine, ...]: ...


class DailyRate:
    """One rate per class per day. The baseline every other plan composes."""

    def __init__(self, rates: dict[VehicleType, Money] | None = None) -> None:
        self._rates = rates or DEFAULT_DAILY_RATES

    def rate_for(self, vehicle_type: VehicleType) -> Money:
        return self._rates[vehicle_type]

    def price(self, vehicle_type: VehicleType, days: int) -> tuple[InvoiceLine, ...]:
        if days <= 0:
            raise ValidationError("a rental is at least one day")
        rate = self.rate_for(vehicle_type)
        label = f"{vehicle_type} rental: {days} day(s) x {rate}"
        return (InvoiceLine(label, rate * days),)


class WeeklyRate:
    """Seven days cost six. Composes ``DailyRate`` instead of subclassing it."""

    def __init__(self, daily: DailyRate, charged_days_per_week: int = 6) -> None:
        self._daily = daily
        self._charged = charged_days_per_week

    def price(self, vehicle_type: VehicleType, days: int) -> tuple[InvoiceLine, ...]:
        if days <= 0:
            raise ValidationError("a rental is at least one day")
        rate = self._daily.rate_for(vehicle_type)
        weeks, extra = divmod(days, 7)
        if not weeks:
            return (InvoiceLine(f"{vehicle_type} rental: {days} day(s) x {rate}", rate * days),)
        label = f"{vehicle_type} rental: {weeks} week(s) x {self._charged} day(s) x {rate}"
        lines = [InvoiceLine(label, rate * (weeks * self._charged))]
        if extra:
            lines.append(InvoiceLine(f"{extra} extra day(s) x {rate}", rate * extra))
        return tuple(lines)


# --8<-- [end:rates]


# --8<-- [start:addons]
class AddOn(ABC):
    """Decorator: wraps any ``RatePlan`` and appends one line to its output.

    Add-ons compose in any order and in any number, which an ``add_gps: bool``
    flag on the reservation cannot do -- and ``InsuranceAddOn`` proves the point,
    because it prices itself off whatever it wraps.
    """

    add_on_type: AddOnType

    def __init__(self, inner: RatePlan) -> None:
        self._inner = inner

    def price(self, vehicle_type: VehicleType, days: int) -> tuple[InvoiceLine, ...]:
        base = self._inner.price(vehicle_type, days)
        return (*base, self.line(base, vehicle_type, days))

    @abstractmethod
    def line(
        self, base: tuple[InvoiceLine, ...], vehicle_type: VehicleType, days: int
    ) -> InvoiceLine: ...


class GpsAddOn(AddOn):
    add_on_type = AddOnType.GPS
    RATE = Money.of("4.00")

    def line(self, base: tuple[InvoiceLine, ...], vehicle_type: VehicleType, days: int) -> InvoiceLine:
        return InvoiceLine(f"GPS unit: {days} day(s) x {self.RATE}", self.RATE * days)


class ChildSeatAddOn(AddOn):
    add_on_type = AddOnType.CHILD_SEAT
    RATE = Money.of("6.50")

    def line(self, base: tuple[InvoiceLine, ...], vehicle_type: VehicleType, days: int) -> InvoiceLine:
        return InvoiceLine(f"child seat: {days} day(s) x {self.RATE}", self.RATE * days)


class InsuranceAddOn(AddOn):
    """Collision damage waiver: a percentage of everything wrapped so far."""

    add_on_type = AddOnType.INSURANCE
    PERCENT = Decimal("0.20")

    def line(self, base: tuple[InvoiceLine, ...], vehicle_type: VehicleType, days: int) -> InvoiceLine:
        subtotal = total_of(base)
        return InvoiceLine("damage waiver: 20% of the lines above", subtotal * self.PERCENT)


class AddOnFactory:
    """Maps the codes stored on a reservation back to decorator classes."""

    _registry: dict[AddOnType, type[AddOn]] = {
        AddOnType.GPS: GpsAddOn,
        AddOnType.CHILD_SEAT: ChildSeatAddOn,
        AddOnType.INSURANCE: InsuranceAddOn,
    }

    @classmethod
    def decorate(cls, plan: RatePlan, add_ons: Iterable[AddOnType | str]) -> RatePlan:
        """Wrap ``plan`` once per add-on. Insurance is applied last on purpose."""
        try:
            codes = [AddOnType(a) for a in add_ons]
        except ValueError as exc:
            raise ValidationError(f"unknown add-on: {exc}") from exc
        for add_on in sorted(codes, key=lambda a: a is AddOnType.INSURANCE):
            plan = cls._registry[add_on](plan)
        return plan


# --8<-- [end:addons]


# --8<-- [start:returns]
class ReturnCharges:
    """Everything you can only price once the car is back on the lot.

    Late days, a refuelling charge per missing eighth of a tank, mileage above
    the daily allowance, the one-way drop-off fee, and the damage assessment.
    """

    LATE_MULTIPLIER = Decimal("1.5")
    FUEL_PER_EIGHTH = Money.of("9.00")
    INCLUDED_KM_PER_DAY = 200
    PER_EXTRA_KM = Money.of("0.25")
    ONE_WAY_FEE = Money.of("75.00")

    def __init__(
        self,
        daily: DailyRate,
        late_multiplier: Decimal | None = None,
        fuel_per_eighth: Money | None = None,
        included_km_per_day: int | None = None,
        per_extra_km: Money | None = None,
        one_way_fee: Money | None = None,
    ) -> None:
        self._daily = daily
        self._late_multiplier = late_multiplier or self.LATE_MULTIPLIER
        self._fuel_per_eighth = fuel_per_eighth or self.FUEL_PER_EIGHTH
        self._included_km_per_day = included_km_per_day or self.INCLUDED_KM_PER_DAY
        self._per_extra_km = per_extra_km or self.PER_EXTRA_KM
        self._one_way_fee = one_way_fee or self.ONE_WAY_FEE

    def lines(self, reservation: Reservation, damage_fee: Money | None = None) -> tuple[InvoiceLine, ...]:
        lines: list[InvoiceLine] = []
        rate = self._daily.rate_for(reservation.vehicle_type)

        late = reservation.late_days
        if late:
            per_day = rate * self._late_multiplier
            lines.append(InvoiceLine(f"late return: {late} day(s) x {per_day}", per_day * late))

        if reservation.pickup_fuel is not None and reservation.return_fuel is not None:
            missing = reservation.pickup_fuel - reservation.return_fuel
            if missing > 0:
                amount = self._fuel_per_eighth * missing
                lines.append(InvoiceLine(f"refuelling: {missing}/8 tank", amount))

        allowance = self._included_km_per_day * (reservation.period.days + late)
        extra_km = reservation.kilometres_driven - allowance
        if extra_km > 0:
            amount = self._per_extra_km * extra_km
            lines.append(InvoiceLine(f"mileage: {extra_km} km past the {allowance} km allowance", amount))

        if reservation.is_one_way:
            lines.append(InvoiceLine(f"one-way drop-off at {reservation.dropoff_branch}", self._one_way_fee))

        if damage_fee is not None and not damage_fee.is_zero():
            lines.append(InvoiceLine("damage assessment", damage_fee))

        return tuple(lines)


# --8<-- [end:returns]
