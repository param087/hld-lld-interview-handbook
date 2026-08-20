"""Floors, the lot, the gates and the display board: where behaviour and locks live."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Protocol

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock
from lld.parking_lot.models import (
    FloorAvailability,
    InvalidTicketError,
    LotFullError,
    ParkingSpot,
    Payment,
    PaymentDeclinedError,
    PaymentMethod,
    SpotStatus,
    SpotType,
    Ticket,
    TicketStateError,
    TicketStatus,
    Vehicle,
    VehicleFactory,
    VehicleType,
)
from lld.parking_lot.strategies import (
    HourlyPricing,
    NearestFirstAllocation,
    PricingStrategy,
    SpotAllocationStrategy,
)

SPOT_PREFIX = {
    SpotType.MOTORCYCLE: "M",
    SpotType.COMPACT: "C",
    SpotType.LARGE: "L",
    SpotType.ELECTRIC: "E",
}


# --8<-- [start:observer]
class AvailabilityListener(Protocol):
    """Observer interface: anything that wants to know when a floor's free counts change."""

    def on_availability_changed(self, availability: FloorAvailability) -> None: ...


class DisplayBoard:
    """The sign at the entrance. It never polls; floors push updates to it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._floors: dict[int, FloorAvailability] = {}

    def on_availability_changed(self, availability: FloorAvailability) -> None:
        with self._lock:
            self._floors[availability.floor] = availability

    def free_spots(self, floor: int) -> int:
        with self._lock:
            return self._floors[floor].total_free() if floor in self._floors else 0

    def render(self) -> str:
        with self._lock:
            lines = []
            for number in sorted(self._floors):
                counts = ", ".join(
                    f"{n} {spot_type.value}" for spot_type, n in self._floors[number].free.items()
                )
                lines.append(f"Floor {number}: {counts}")
            return "\n".join(lines)


# --8<-- [end:observer]


# --8<-- [start:floor]
class ParkingFloor:
    """Owns its spots and the lock that makes assignment atomic *per floor*.

    Two gates racing for the last spot on this floor serialise here; gates
    working on different floors never contend with each other.
    """

    def __init__(self, number: int, spots: Iterable[ParkingSpot]) -> None:
        self.number = number
        self._spots: dict[str, ParkingSpot] = {s.id: s for s in spots}
        self._lock = threading.Lock()
        self._listeners: list[AvailabilityListener] = []

    @classmethod
    def build(cls, number: int, layout: dict[SpotType, int]) -> ParkingFloor:
        """``ParkingFloor.build(1, {SpotType.COMPACT: 3, SpotType.LARGE: 1})``."""
        spots = [
            ParkingSpot(id=f"F{number}-{SPOT_PREFIX[spot_type]}{i:02d}", floor=number, type=spot_type)
            for spot_type, count in layout.items()
            for i in range(1, count + 1)
        ]
        return cls(number, spots)

    def subscribe(self, listener: AvailabilityListener) -> None:
        self._listeners.append(listener)
        listener.on_availability_changed(self.availability())

    def spot(self, spot_id: str) -> ParkingSpot:
        return self._spots[spot_id]

    def first_free(self, spot_type: SpotType) -> ParkingSpot | None:
        """A *hint* read without the lock; ``try_assign`` re-checks under the lock."""
        for spot in self._spots.values():  # insertion order == lowest id first
            if spot.type is spot_type and spot.is_free():
                return spot
        return None

    def try_assign(self, spot_id: str, vehicle: Vehicle) -> bool:
        """Claim the spot atomically. Returns False if someone else got it first."""
        with self._lock:
            spot = self._spots[spot_id]
            if not spot.is_free():
                return False
            spot.assign(vehicle)
            availability = self.availability()
        self._notify(availability)
        return True

    def release(self, spot_id: str) -> None:
        with self._lock:
            self._spots[spot_id].release()
            availability = self.availability()
        self._notify(availability)

    def set_out_of_service(self, spot_id: str) -> None:
        with self._lock:
            spot = self._spots[spot_id]
            if spot.status is SpotStatus.OCCUPIED:
                raise TicketStateError(f"spot {spot_id} is occupied")
            spot.status = SpotStatus.OUT_OF_SERVICE
            availability = self.availability()
        self._notify(availability)

    def availability(self) -> FloorAvailability:
        free: dict[SpotType, int] = {}
        for spot in self._spots.values():
            free.setdefault(spot.type, 0)
            if spot.is_free():
                free[spot.type] += 1
        return FloorAvailability(self.number, free)

    def _notify(self, availability: FloorAvailability) -> None:
        # Outside the lock: a slow listener must never block a gate.
        for listener in self._listeners:
            listener.on_availability_changed(availability)


# --8<-- [end:floor]


# --8<-- [start:lot]
class ParkingLot:
    """The aggregate root: floors + tickets. Built once in ``main`` and injected into gates.

    Deliberately *not* a Singleton class: tests build many lots, and a second
    physical lot is then one more object rather than a redesign.
    """

    MAX_CLAIM_ATTEMPTS = 64

    def __init__(
        self,
        name: str,
        floors: Iterable[ParkingFloor],
        allocation: SpotAllocationStrategy | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self.name = name
        self._floors = {f.number: f for f in floors}
        self._allocation = allocation or NearestFirstAllocation()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("T")
        self._tickets: dict[str, Ticket] = {}
        self._tickets_lock = threading.Lock()

    def floors(self) -> list[ParkingFloor]:
        return list(self._floors.values())

    def park(self, vehicle: Vehicle) -> Ticket:
        """Choose a spot optimistically, then claim it atomically; retry if a gate beat us to it."""
        for _ in range(self.MAX_CLAIM_ATTEMPTS):
            spot = self._allocation.choose(self.floors(), vehicle)
            if spot is None:
                raise LotFullError(f"no free spot for {vehicle!r} in {self.name}")
            if self._floors[spot.floor].try_assign(spot.id, vehicle):
                ticket = Ticket(
                    id=self._ids.next_id(),
                    plate=vehicle.plate,
                    vehicle_type=vehicle.vehicle_type,
                    spot_id=spot.id,
                    floor=spot.floor,
                    entry_time=self._clock.now(),
                )
                with self._tickets_lock:
                    self._tickets[ticket.id] = ticket
                return ticket
        raise LotFullError("could not claim a spot under heavy contention; try again")

    def ticket(self, ticket_id: str) -> Ticket:
        with self._tickets_lock:
            try:
                return self._tickets[ticket_id]
            except KeyError:
                raise InvalidTicketError(f"unknown ticket {ticket_id}") from None

    def active_ticket_for_plate(self, plate: str) -> Ticket | None:
        plate = plate.strip().upper()
        with self._tickets_lock:
            for ticket in self._tickets.values():
                if ticket.plate == plate and ticket.status is TicketStatus.ACTIVE:
                    return ticket
        return None

    def begin_checkout(self, ticket_id: str) -> Ticket:
        """ACTIVE -> PAYING. Two exit gates cannot both charge the same ticket."""
        with self._tickets_lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise InvalidTicketError(f"unknown ticket {ticket_id}")
            if ticket.status is not TicketStatus.ACTIVE:
                raise TicketStateError(f"ticket {ticket_id} is {ticket.status}, not active")
            ticket.status = TicketStatus.PAYING
            return ticket

    def abort_checkout(self, ticket: Ticket) -> None:
        with self._tickets_lock:
            ticket.status = TicketStatus.ACTIVE

    def complete_checkout(self, ticket: Ticket, exit_time: float, fee: Money, status: TicketStatus) -> None:
        """PAYING -> PAID/LOST and free the spot."""
        with self._tickets_lock:
            if ticket.status is not TicketStatus.PAYING:
                raise TicketStateError(f"ticket {ticket.id} is {ticket.status}, not paying")
            ticket.exit_time = exit_time
            ticket.fee = fee
            ticket.status = status
        self._floors[ticket.floor].release(ticket.spot_id)

    def availability(self) -> list[FloorAvailability]:
        return [f.availability() for f in self.floors()]

    def free_spots(self) -> int:
        return sum(a.total_free() for a in self.availability())


# --8<-- [end:lot]


# --8<-- [start:gates]
class EntryGate:
    def __init__(self, gate_id: str, lot: ParkingLot) -> None:
        self.gate_id = gate_id
        self._lot = lot

    def issue_ticket(self, vehicle_type: VehicleType | str, plate: str) -> Ticket:
        vehicle = VehicleFactory.create(vehicle_type, plate)
        return self._lot.park(vehicle)


class PaymentProcessor(Protocol):
    def charge(self, amount: Money, method: PaymentMethod) -> bool: ...


class AlwaysApprovesProcessor:
    """Stand-in for the card terminal / cash drawer."""

    def charge(self, amount: Money, method: PaymentMethod) -> bool:
        return True


class ExitGate:
    LOST_TICKET_FEE = Money.of("50.00")

    def __init__(
        self,
        gate_id: str,
        lot: ParkingLot,
        pricing: PricingStrategy | None = None,
        processor: PaymentProcessor | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self.gate_id = gate_id
        self._lot = lot
        self._pricing = pricing or HourlyPricing()
        self._processor = processor or AlwaysApprovesProcessor()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("P")

    def quote(self, ticket_id: str) -> Money:
        ticket = self._lot.ticket(ticket_id)
        return self._pricing.calculate(ticket.vehicle_type, ticket.duration_seconds(self._clock.now()))

    def process(self, ticket_id: str, method: PaymentMethod) -> Payment:
        """Reserve the ticket, charge, then commit — so a declined card leaves the car parked."""
        ticket = self._lot.begin_checkout(ticket_id)
        now = self._clock.now()
        fee = self._pricing.calculate(ticket.vehicle_type, ticket.duration_seconds(now))
        return self._settle(ticket, fee, method, now, TicketStatus.PAID)

    def process_lost_ticket(self, plate: str, method: PaymentMethod) -> Payment:
        ticket = self._lot.active_ticket_for_plate(plate)
        if ticket is None:
            raise InvalidTicketError(f"no active ticket for plate {plate}")
        self._lot.begin_checkout(ticket.id)
        return self._settle(ticket, self.LOST_TICKET_FEE, method, self._clock.now(), TicketStatus.LOST)

    def _settle(
        self, ticket: Ticket, fee: Money, method: PaymentMethod, now: float, status: TicketStatus
    ) -> Payment:
        if not fee.is_zero() and not self._processor.charge(fee, method):
            self._lot.abort_checkout(ticket)
            raise PaymentDeclinedError(f"{method} payment of {fee} declined for ticket {ticket.id}")
        self._lot.complete_checkout(ticket, exit_time=now, fee=fee, status=status)
        return Payment(id=self._ids.next_id(), ticket_id=ticket.id, amount=fee, method=method, paid_at=now)


# --8<-- [end:gates]
