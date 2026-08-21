"""Branches, the fleet pool and the ``RentalSystem`` facade -- where the locks live."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from datetime import date, timedelta

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock, ValidationError
from lld.car_rental.models import (
    UPGRADE_LADDER,
    AddOnType,
    DateRange,
    Invoice,
    InvoiceLine,
    MaintenanceRecord,
    NoVehicleAvailableError,
    Payment,
    PaymentMethod,
    Reservation,
    ReservationStateError,
    ReservationStatus,
    UnknownBranchError,
    UnknownReservationError,
    UnknownVehicleError,
    Vehicle,
    VehicleStatus,
    VehicleType,
)
from lld.car_rental.strategies import (
    AddOnFactory,
    AlwaysApprovesProcessor,
    DailyRate,
    PaymentProcessor,
    RatePlan,
    ReturnCharges,
    WeeklyRate,
)

SERVICE_DAYS_AFTER_DAMAGE = 3


# --8<-- [start:pool]
class FleetPool:
    """Object Pool over one branch's cars: checked out at pickup, checked in at return.

    Cars are expensive and finite, so the desk never *creates* one -- it borrows
    the next serviceable car of an acceptable class and gives it back. The pool
    takes no lock of its own: ``Branch`` serialises every call (see below), which
    is what keeps the calendars and the booking ledger consistent with each other.
    """

    def __init__(self, vehicles: Iterable[Vehicle] = ()) -> None:
        self._by_plate: dict[str, Vehicle] = {v.plate: v for v in vehicles}

    def add(self, vehicle: Vehicle) -> None:
        self._by_plate[vehicle.plate] = vehicle

    def remove(self, plate: str) -> Vehicle:
        try:
            return self._by_plate.pop(plate)
        except KeyError:
            raise UnknownVehicleError(f"no vehicle {plate}") from None

    def get(self, plate: str) -> Vehicle:
        try:
            return self._by_plate[plate]
        except KeyError:
            raise UnknownVehicleError(f"no vehicle {plate}") from None

    def serviceable(self, vehicle_type: VehicleType) -> list[Vehicle]:
        """Cars of that class still on the books (a retired car is gone forever)."""
        return [
            v
            for v in self._by_plate.values()
            if v.vehicle_type is vehicle_type and v.status is not VehicleStatus.RETIRED
        ]

    def acquire(self, classes: Sequence[VehicleType], period: DateRange, label: str) -> Vehicle:
        """Borrow the first free car, walking the upgrade ladder if the booked class is out."""
        for vehicle_type in classes:
            for vehicle in self.serviceable(vehicle_type):
                if vehicle.status is VehicleStatus.AVAILABLE and vehicle.calendar.is_free(period):
                    vehicle.calendar.block(label, period)
                    vehicle.status = VehicleStatus.RENTED
                    return vehicle
        raise NoVehicleAvailableError(f"no car of {list(classes)} free for {period}")

    def release(self, plate: str, needs_service: bool, today: date) -> Vehicle:
        """Give the car back. Damage sends it to the workshop and blocks its calendar."""
        vehicle = self.get(plate)
        if not needs_service:
            vehicle.status = VehicleStatus.AVAILABLE
            return vehicle
        vehicle.status = VehicleStatus.MAINTENANCE
        window = DateRange(today, today + timedelta(days=SERVICE_DAYS_AFTER_DAMAGE))
        vehicle.calendar.block_or_merge(f"service:{plate}", window)
        return vehicle


# --8<-- [end:pool]


# --8<-- [start:branch]
class Branch:
    """One rental location: its fleet, its booking ledger, and *the* lock over both.

    ``Branch._lock`` is the serialisation point of the whole design. It is coarser
    than one lock per car (because "is anything of this class free?" would then be
    a multi-lock scan) and far finer than one lock for the company (two cities
    never contend). Everything it guards has to agree at all times: the counted
    availability for a class, the ledger of holds, and every vehicle calendar.
    """

    def __init__(self, branch_id: str, city: str, vehicles: Iterable[Vehicle] = ()) -> None:
        self.id = branch_id
        self.city = city
        self._pool = FleetPool(vehicles)
        self._bookings: dict[str, Reservation] = {}
        self.lock = threading.RLock()

    def fleet_size(self, vehicle_type: VehicleType) -> int:
        with self.lock:
            return len(self._pool.serviceable(vehicle_type))

    def available(self, vehicle_type: VehicleType, period: DateRange) -> int:
        with self.lock:
            return self._available(vehicle_type, period)

    def _available(self, vehicle_type: VehicleType, period: DateRange) -> int:
        """Cars of the class, minus those with a clashing calendar block, minus open holds.

        A picked-up reservation is counted exactly once: as a calendar block on
        the car it was pinned to. Only ``RESERVED`` rows are counted as holds,
        which is why the two terms never double-count each other.
        """
        fleet = self._pool.serviceable(vehicle_type)
        blocked = sum(1 for v in fleet if not v.calendar.is_free(period))
        held = sum(
            1
            for r in self._bookings.values()
            if r.vehicle_type is vehicle_type
            and r.status is ReservationStatus.RESERVED
            and r.period.overlaps(period)
        )
        return len(fleet) - blocked - held

    def hold(self, reservation: Reservation) -> None:
        """Atomically re-check capacity and record the hold. The oversell guard."""
        with self.lock:
            if self._available(reservation.vehicle_type, reservation.period) <= 0:
                raise NoVehicleAvailableError(
                    f"no {reservation.vehicle_type} free at {self.id} for {reservation.period}"
                )
            self._bookings[reservation.id] = reservation

    def release_hold(self, reservation_id: str) -> None:
        with self.lock:
            self._bookings.pop(reservation_id, None)

    def check_out(self, reservation: Reservation) -> Vehicle:
        """Pin a plate to the reservation, upgrading if the booked class is out."""
        with self.lock:
            ladder = UPGRADE_LADDER[reservation.vehicle_type]
            vehicle = self._pool.acquire(ladder, reservation.period, reservation.id)
            self._bookings.pop(reservation.id, None)
            return vehicle

    def check_in(
        self, reservation: Reservation, return_date: date, needs_service: bool
    ) -> tuple[Vehicle, list[str]]:
        """Close the calendar block (extending it first if the car is late) and free the car."""
        with self.lock:
            plate = reservation.plate or ""
            vehicle = self._pool.get(plate)
            displaced = vehicle.calendar.extend(reservation.id, return_date)
            vehicle.calendar.unblock(reservation.id)
            if reservation.return_odometer is not None:
                vehicle.odometer_km = reservation.return_odometer
            if reservation.return_fuel is not None:
                vehicle.fuel_eighths = reservation.return_fuel
            self._pool.release(plate, needs_service, return_date)
            return vehicle, displaced

    def schedule_maintenance(self, record: MaintenanceRecord) -> MaintenanceRecord:
        """Block a car's calendar for a service window; overlaps are rejected outright."""
        with self.lock:
            vehicle = self._pool.get(record.plate)
            vehicle.calendar.block(f"maint:{record.id}", record.period)
            return record

    def adopt(self, vehicle: Vehicle) -> None:
        with self.lock:
            vehicle.branch_id = self.id
            self._pool.add(vehicle)

    def surrender(self, plate: str) -> Vehicle:
        with self.lock:
            return self._pool.remove(plate)


def transfer(source: Branch, target: Branch, plate: str) -> Vehicle:
    """Move a car between branches after a one-way rental.

    Both locks are needed, so they are taken in branch-id order: two one-way
    returns running in opposite directions can then never deadlock.
    """
    first, second = sorted((source, target), key=lambda b: b.id)
    with first.lock, second.lock:
        vehicle = source.surrender(plate)
        target.adopt(vehicle)
        return vehicle


# --8<-- [end:branch]


# --8<-- [start:system]
class RentalSystem:
    """Facade: search, reserve, cancel, pick up, return, pay -- one object per company.

    Two locks, never nested: this one guards the reservation registry and every
    status transition; ``Branch.lock`` guards fleet and capacity. A transition is
    *claimed* under this lock, the branch work happens outside it, and the claim
    is reverted if that work fails -- the same reserve/do/commit shape the parking
    lot uses at its exit gate.
    """

    FREE_CANCELLATION_DAYS = 1

    def __init__(
        self,
        branches: Iterable[Branch],
        rate_plan: RatePlan | None = None,
        charges: ReturnCharges | None = None,
        processor: PaymentProcessor | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        invoice_ids: IdGenerator | None = None,
        maintenance_ids: IdGenerator | None = None,
    ) -> None:
        daily = DailyRate()
        self._branches = {b.id: b for b in branches}
        self._rate_plan = rate_plan or WeeklyRate(daily)
        self._charges = charges or ReturnCharges(daily)
        self._daily = daily
        self._processor = processor or AlwaysApprovesProcessor()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("R")
        self._invoice_ids = invoice_ids or SequentialIdGenerator("INV")
        self._maintenance_ids = maintenance_ids or SequentialIdGenerator("M")
        self._reservations: dict[str, Reservation] = {}
        self._lock = threading.Lock()

    def today(self) -> date:
        return self._clock.now_dt().date()

    def branch(self, branch_id: str) -> Branch:
        try:
            return self._branches[branch_id]
        except KeyError:
            raise UnknownBranchError(f"no branch {branch_id}") from None

    def reservation(self, reservation_id: str) -> Reservation:
        with self._lock:
            try:
                return self._reservations[reservation_id]
            except KeyError:
                raise UnknownReservationError(f"unknown reservation {reservation_id}") from None

    def search(self, city: str, vehicle_type: VehicleType, period: DateRange) -> list[tuple[str, int]]:
        """Branches in the city with at least one car of the class free for the whole range."""
        found = [
            (b.id, b.available(vehicle_type, period))
            for b in self._branches.values()
            if b.city.casefold() == city.casefold()
        ]
        return sorted([(bid, n) for bid, n in found if n > 0], key=lambda row: (-row[1], row[0]))

    def quote(self, vehicle_type: VehicleType, period: DateRange, add_ons: Iterable[AddOnType] = ()) -> Invoice:
        """Price a rental before it exists -- the same code path the invoice uses."""
        plan = AddOnFactory.decorate(self._rate_plan, add_ons)
        return Invoice(id="quote", reservation_id="-", lines=plan.price(vehicle_type, period.days))

    def reserve(
        self,
        customer_id: str,
        vehicle_type: VehicleType,
        pickup_branch: str,
        period: DateRange,
        dropoff_branch: str | None = None,
        add_ons: Iterable[AddOnType] = (),
    ) -> Reservation:
        if period.start < self.today():
            raise ValidationError(f"cannot book {period}: it starts in the past")
        drop = dropoff_branch or pickup_branch
        branch, target = self.branch(pickup_branch), self.branch(drop)
        if branch.available(VehicleType(vehicle_type), period) <= 0:  # cheap hint; hold() is the truth
            raise NoVehicleAvailableError(f"no {vehicle_type} free at {branch.id} for {period}")
        reservation = Reservation(
            id=self._ids.next_id(),
            customer_id=customer_id,
            vehicle_type=VehicleType(vehicle_type),
            pickup_branch=branch.id,
            dropoff_branch=target.id,
            period=period,
            add_ons=tuple(AddOnType(a) for a in add_ons),
        )
        branch.hold(reservation)  # raises NoVehicleAvailableError; nothing recorded yet
        with self._lock:
            self._reservations[reservation.id] = reservation
        return reservation

    def cancel(self, reservation_id: str) -> Money:
        """Free the counted slot. Cancelling inside the free window costs one day."""
        reservation = self._claim(reservation_id, ReservationStatus.RESERVED, ReservationStatus.CANCELLED)
        self.branch(reservation.pickup_branch).release_hold(reservation.id)
        days_ahead = (reservation.period.start - self.today()).days
        if days_ahead >= self.FREE_CANCELLATION_DAYS:
            return Money(0)
        return self._daily.rate_for(reservation.vehicle_type)

    def mark_no_show(self, reservation_id: str) -> Reservation:
        reservation = self._claim(reservation_id, ReservationStatus.RESERVED, ReservationStatus.NO_SHOW)
        self.branch(reservation.pickup_branch).release_hold(reservation.id)
        return reservation

    def pick_up(self, reservation_id: str) -> Vehicle:
        """RESERVED -> PICKED_UP, then pin a plate. A failed hand-over rolls the status back."""
        reservation = self._claim(reservation_id, ReservationStatus.RESERVED, ReservationStatus.PICKED_UP)
        try:
            vehicle = self.branch(reservation.pickup_branch).check_out(reservation)
        except NoVehicleAvailableError:
            self._revert(reservation, ReservationStatus.RESERVED)
            raise
        reservation.plate = vehicle.plate
        reservation.handed_over_type = vehicle.vehicle_type
        reservation.pickup_odometer = vehicle.odometer_km
        reservation.pickup_fuel = vehicle.fuel_eighths
        return vehicle

    def return_vehicle(
        self,
        reservation_id: str,
        return_date: date,
        odometer_km: int,
        fuel_eighths: int,
        damage_fee: Money | None = None,
    ) -> tuple[Invoice, list[str]]:
        """PICKED_UP -> RETURNED. Returns the invoice and any service slots the car ate into."""
        reservation = self._claim(reservation_id, ReservationStatus.PICKED_UP, ReservationStatus.RETURNED)
        if return_date < reservation.period.start:
            self._revert(reservation, ReservationStatus.PICKED_UP)
            raise ValidationError(f"return date {return_date} precedes pickup {reservation.period.start}")
        reservation.return_date = return_date
        reservation.return_odometer = odometer_km
        reservation.return_fuel = fuel_eighths

        pickup = self.branch(reservation.pickup_branch)
        damaged = damage_fee is not None and not damage_fee.is_zero()
        _, displaced = pickup.check_in(reservation, return_date, needs_service=damaged)
        if reservation.is_one_way:
            transfer(pickup, self.branch(reservation.dropoff_branch), reservation.plate or "")

        plan = AddOnFactory.decorate(self._rate_plan, reservation.add_ons)
        lines: list[InvoiceLine] = list(plan.price(reservation.vehicle_type, reservation.period.days))
        lines.extend(self._charges.lines(reservation, damage_fee))
        return Invoice(self._invoice_ids.next_id(), reservation.id, tuple(lines)), displaced

    def schedule_maintenance(self, branch_id: str, plate: str, period: DateRange, reason: str) -> MaintenanceRecord:
        record = MaintenanceRecord(self._maintenance_ids.next_id(), plate.upper(), period, reason)
        return self.branch(branch_id).schedule_maintenance(record)

    def pay(self, invoice: Invoice, method: PaymentMethod) -> Payment:
        if not self._processor.charge(invoice.total, method):
            raise ReservationStateError(f"{method} payment of {invoice.total} declined")
        return Payment(f"PAY-{invoice.id}", invoice.id, invoice.total, method, self._clock.now())

    def _claim(self, reservation_id: str, expected: ReservationStatus, target: ReservationStatus) -> Reservation:
        """Check-and-flip the status atomically so two desks cannot both act on it."""
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None:
                raise UnknownReservationError(f"unknown reservation {reservation_id}")
            if reservation.status is not expected:
                raise ReservationStateError(
                    f"reservation {reservation_id} is {reservation.status}, expected {expected}"
                )
            reservation.status = target
            return reservation

    def _revert(self, reservation: Reservation, status: ReservationStatus) -> None:
        with self._lock:
            reservation.status = status


# --8<-- [end:system]
