"""Inventory reservation with a TTL and version checks, a sharded flash-sale counter, and the
checkout saga that ties order, payment, inventory and fulfilment together through an outbox.

What the module demonstrates, in the order an interviewer asks about it:

* ``InventoryService.reserve`` takes every line of an order or none, moving units from
  ``on_hand`` into ``reserved`` and bumping each SKU's ``version``. It is idempotent per
  ``order_id`` while the hold is **live** -- held or already committed -- so a retried checkout
  never reserves twice, while a checkout retried after a compensation gets a fresh hold rather
  than the dead one it released.
* ``commit`` turns a live reservation into a sale; ``release`` and ``expire`` give the units
  back, and an expired reservation is reclaimed lazily by the next reserve as well as by the
  sweeper, so stuck stock is impossible.
* ``FlashSaleCounter`` splits one contended counter into N shards, so a hundred-thousand-buyer
  drop does not serialise on a single row.
* ``build_checkout_saga`` composes :mod:`hld.saga`: reserve (compensatable), charge (the pivot),
  then commit and ship (retriable), each appending to an ``Outbox`` in the same critical section
  as its state change.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from common import (
    Clock,
    ConflictError,
    IdGenerator,
    InvalidStateError,
    Money,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)
from hld.saga import SagaContext, SagaLog, SagaOrchestrator, Step, StepFailed, StepKind


# --8<-- [start:models]
class ReservationState(StrEnum):
    HELD = "held"  # units moved from on_hand into reserved, TTL running
    COMMITTED = "committed"  # the sale went through: units left the warehouse
    RELEASED = "released"  # cart abandoned or a saga compensation gave them back
    EXPIRED = "expired"  # the TTL ran out before checkout completed


class OrderState(StrEnum):
    CREATED = "created"
    RESERVED = "reserved"
    PAID = "paid"
    FULFILLING = "fulfilling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class StockItem:
    sku: str
    on_hand: int
    reserved: int = 0
    version: int = 0  # bumped by every reserve, release and commit: the optimistic lock

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


@dataclass(slots=True)
class Reservation:
    reservation_id: str
    order_id: str
    lines: dict[str, int]  # sku -> quantity
    expires_at: float
    # Audit-only: the stock versions this hold was taken against, carried on the reserve event.
    # Deliberately *not* re-checked by commit -- see InventoryService.commit for why a hold
    # needs no version check, unlike a seat hold in hld.seat_hold, whose seats can be taken
    # over the moment its TTL lapses.
    versions: dict[str, int] = field(default_factory=dict)
    state: ReservationState = ReservationState.HELD

    @property
    def is_live(self) -> bool:
        """Held, or the sale it already became: the two states a retried checkout may reuse."""
        return self.state in (ReservationState.HELD, ReservationState.COMMITTED)


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    topic: str
    payload: Mapping[str, Any]


# --8<-- [end:models]


# --8<-- [start:outbox]
class Outbox:
    """Events written next to the state change they describe, then relayed to the broker.

    ``_lock`` guards ``_pending``. In production this is a table in the service's own database
    appended inside the business transaction, so either the row and the event both exist or
    neither does. A relay tails it and publishes: no dual write.
    """

    def __init__(self) -> None:
        self._pending: list[OutboxEvent] = []
        self._lock = threading.Lock()

    def append(self, topic: str, **payload: Any) -> None:
        with self._lock:
            self._pending.append(OutboxEvent(topic, dict(payload)))

    def relay(self) -> list[OutboxEvent]:
        """Publish everything pending. Re-publishing after a crash is fine: consumers dedupe."""
        with self._lock:
            batch, self._pending = self._pending, []
            return batch


# --8<-- [end:outbox]


# --8<-- [start:inventory]
class InventoryService:
    """Reservations over a stock table. ``_lock`` guards ``_stock`` and ``_reservations``.

    Each method below is one database transaction. ``reserve`` is the twin of ``UPDATE stock
    SET reserved = reserved + ?, version = version + 1 WHERE sku = ? AND on_hand - reserved >= ?``
    repeated per line, committed only when every statement touched a row.
    """

    def __init__(self, clock: Clock, ids: IdGenerator | None = None, hold_ttl: float = 900.0) -> None:
        if hold_ttl <= 0:
            raise ValidationError("hold_ttl must be positive")
        self._clock = clock
        self._ids = ids or SequentialIdGenerator("rsv")
        self._ttl = hold_ttl
        self._stock: dict[str, StockItem] = {}
        self._reservations: dict[str, Reservation] = {}
        self._by_order: dict[str, str] = {}  # order_id -> reservation_id: the idempotency key
        self._lock = threading.Lock()

    def stock_in(self, sku: str, quantity: int) -> StockItem:
        if quantity <= 0:
            raise ValidationError("quantity must be positive")
        with self._lock:
            item = self._stock.setdefault(sku, StockItem(sku, 0))
            item.on_hand, item.version = item.on_hand + quantity, item.version + 1
            return item

    # -- write path ---------------------------------------------------------------------------
    def reserve(self, order_id: str, lines: Mapping[str, int], expected_versions: Mapping[str, int] | None = None) -> Reservation:
        """Reserve every line or none; twice for one order returns the same **live** reservation.
        Passing ``expected_versions`` from a prior :meth:`versions` read makes it a compare-and-set.

        Order matters here. The sweep runs *first*, so a hold whose TTL lapsed but which no
        sweeper has touched yet is expired before it can be mistaken for a live one. Only then
        is the idempotency key consulted, and only a held or committed reservation counts as a
        retry: a saga that compensated left its reservation ``RELEASED``, and handing that dead
        hold back to a retried checkout would give the caller units it no longer owns and a
        :meth:`commit` that can only fail."""
        if not lines or any(qty <= 0 for qty in lines.values()):
            raise ValidationError("every line needs a positive quantity")
        now = self._clock.now()
        with self._lock:
            self._reclaim(now)  # sweep before the idempotency check, never after
            existing = self._by_order.get(order_id)
            if existing is not None and self._reservations[existing].is_live:
                return self._reservations[existing]  # the retried checkout
            unknown = [sku for sku in lines if sku not in self._stock]
            if unknown:
                raise NotFoundError(f"unknown skus: {sorted(unknown)}")
            stale = [s for s, v in (expected_versions or {}).items() if self._stock[s].version != v]
            if stale:  # somebody moved the row between the read and the write
                raise ConflictError(f"stale stock version for {sorted(stale)}; re-read and retry")
            short = [sku for sku, qty in lines.items() if self._stock[sku].available < qty]
            if short:  # one statement touched zero rows: roll the whole reservation back
                raise ConflictError(f"insufficient stock for {sorted(short)}")
            reservation = Reservation(self._ids.next_id(), order_id, dict(lines), now + self._ttl)
            for sku, qty in lines.items():
                item = self._stock[sku]
                item.reserved, item.version = item.reserved + qty, item.version + 1
                reservation.versions[sku] = item.version
            self._reservations[reservation.reservation_id] = reservation
            self._by_order[order_id] = reservation.reservation_id
            return reservation

    def commit(self, reservation_id: str) -> Reservation:
        """Turn a hold into a sale. No version check is needed and that is the point: the units
        are already out of ``available``, so nobody else could have taken them. Only the
        reservation's own state can stop a commit -- expired or released means the units went
        back on the shelf and may already be sold to somebody else. ``reservation.versions`` is
        an audit record published on the reserve event, never a precondition read here."""
        with self._lock:
            reservation = self._get(reservation_id)
            if reservation.state is ReservationState.COMMITTED:
                return reservation  # a retried saga step
            if reservation.state is not ReservationState.HELD:
                # Expired or released: its units are already back on the shelf and may be sold.
                raise InvalidStateError(f"reservation {reservation_id} is {reservation.state}")
            for sku, qty in reservation.lines.items():
                item = self._stock[sku]
                item.on_hand, item.reserved = item.on_hand - qty, item.reserved - qty
                item.version += 1
            reservation.state = ReservationState.COMMITTED
            return reservation

    def release(self, reservation_id: str) -> None:
        """Give the units back. Idempotent, because a saga compensation may run twice."""
        with self._lock:
            reservation = self._get(reservation_id)
            if reservation.state is ReservationState.COMMITTED:
                raise InvalidStateError(f"reservation {reservation_id} is already committed")
            if reservation.state is ReservationState.HELD:
                self._give_back(reservation, ReservationState.RELEASED)

    def expire(self) -> int:
        """The sweeper. Lazy reclamation in ``reserve`` makes this hygiene, not correctness."""
        with self._lock:
            return self._reclaim(self._clock.now())

    def _reclaim(self, now: float) -> int:
        held = ReservationState.HELD
        stale = [r for r in self._reservations.values() if r.state is held and r.expires_at <= now]
        for reservation in stale:
            self._give_back(reservation, ReservationState.EXPIRED)
        return len(stale)

    def _give_back(self, reservation: Reservation, state: ReservationState) -> None:
        for sku, qty in reservation.lines.items():
            item = self._stock[sku]
            item.reserved, item.version = item.reserved - qty, item.version + 1
        reservation.state = state

    def _get(self, reservation_id: str) -> Reservation:
        if reservation_id not in self._reservations:
            raise NotFoundError(f"unknown reservation {reservation_id}")
        return self._reservations[reservation_id]

    # -- read path ----------------------------------------------------------------------------
    def available(self, sku: str) -> int:
        with self._lock:
            if sku not in self._stock:
                raise NotFoundError(f"unknown sku {sku}")
            return self._stock[sku].available

    def versions(self, *skus: str) -> dict[str, int]:
        """Read the optimistic-lock versions to pass back into :meth:`reserve`."""
        with self._lock:
            return {sku: self._stock[sku].version for sku in skus if sku in self._stock}

    def snapshot(self) -> dict[str, tuple[int, int]]:
        """What the product page shows: (available, reserved) per SKU, cached and slightly stale."""
        with self._lock:
            return {sku: (item.available, item.reserved) for sku, item in self._stock.items()}


# --8<-- [end:inventory]


# --8<-- [start:flash_sale]
class FlashSaleCounter:
    """One hot SKU split into N counters so buyers do not queue behind a single row.

    ``_lock`` guards ``_shards``. In production each shard is its own Redis key or row, so
    contention drops by the shard count; the cost is that a sell-out is only visible once every
    shard is empty, which is why ``take`` walks them all before giving up.
    """

    def __init__(self, sku: str, total: int, shards: int = 8) -> None:
        if total <= 0 or shards <= 0:
            raise ValidationError("total and shards must be positive")
        self.sku = sku
        self._shards = [total // shards + (1 if i < total % shards else 0) for i in range(shards)]
        self._cursor = 0
        self._lock = threading.Lock()

    def take(self, quantity: int = 1) -> int | None:
        """Claim ``quantity`` units from some shard; return its index, or None when sold out."""
        if quantity <= 0:
            raise ValidationError("quantity must be positive")
        with self._lock:
            count = len(self._shards)
            for offset in range(count):
                index = (self._cursor + offset) % count
                if self._shards[index] >= quantity:
                    self._shards[index] -= quantity
                    self._cursor = (index + 1) % count
                    return index
            return None  # every shard is empty: the drop is sold out

    def give_back(self, index: int, quantity: int = 1) -> None:
        with self._lock:
            self._shards[index] += quantity

    def remaining(self) -> int:
        with self._lock:
            return sum(self._shards)


# --8<-- [end:flash_sale]


# --8<-- [start:checkout]
Charger = Callable[[str, Money], str]
Refunder = Callable[[str], None]


def build_checkout_saga(
    inventory: InventoryService,
    outbox: Outbox,
    charge: Charger,
    refund: Refunder,
    log: SagaLog | None = None,
) -> SagaOrchestrator:
    """Reserve (undoable), charge (the pivot), then commit and ship (must eventually succeed).

    ``charge`` raises :class:`hld.saga.StepFailed` for a decline and ``TransientError`` for a
    timeout, which is what tells the orchestrator to compensate rather than retry.
    """

    def reserve_inventory(ctx: SagaContext) -> None:
        try:
            reservation = inventory.reserve(ctx["order_id"], ctx["lines"])
        except (ConflictError, NotFoundError) as exc:
            raise StepFailed(str(exc)) from exc
        ctx["reservation_id"] = reservation.reservation_id
        ctx["state"] = OrderState.RESERVED
        outbox.append(
            "inventory-reserved",
            order_id=ctx["order_id"],
            lines=dict(ctx["lines"]),
            versions=dict(reservation.versions),  # the audit trail: which stock rows this took
        )

    def release_inventory(ctx: SagaContext) -> None:
        if "reservation_id" in ctx:
            inventory.release(ctx["reservation_id"])
        ctx["state"] = OrderState.CANCELLED
        outbox.append("order-cancelled", order_id=ctx["order_id"])

    def charge_payment(ctx: SagaContext) -> None:
        ctx["payment_ref"] = charge(ctx["order_id"], ctx["amount"])
        ctx["state"] = OrderState.PAID
        outbox.append("payment-captured", order_id=ctx["order_id"], ref=ctx["payment_ref"])

    def refund_payment(ctx: SagaContext) -> None:
        if "payment_ref" in ctx:
            refund(ctx["payment_ref"])

    def commit_inventory(ctx: SagaContext) -> None:
        inventory.commit(ctx["reservation_id"])
        ctx["state"] = OrderState.FULFILLING
        outbox.append("inventory-committed", order_id=ctx["order_id"])

    def create_shipment(ctx: SagaContext) -> None:
        ctx["shipment_id"] = f"shp-{ctx['order_id']}"
        ctx["state"] = OrderState.COMPLETED
        outbox.append("shipment-created", order_id=ctx["order_id"], shipment=ctx["shipment_id"])

    steps = [
        Step("reserve_inventory", reserve_inventory, release_inventory, StepKind.COMPENSATABLE),
        Step("charge_payment", charge_payment, refund_payment, StepKind.PIVOT),
        Step("commit_inventory", commit_inventory, kind=StepKind.RETRIABLE),
        Step("create_shipment", create_shipment, kind=StepKind.RETRIABLE),
    ]
    return SagaOrchestrator(steps, log or SagaLog())


# --8<-- [end:checkout]


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=1_000.0)
    inv = InventoryService(clock, SequentialIdGenerator("rsv"), hold_ttl=900)
    for sku, qty in (("tshirt", 5), ("mug", 2), ("poster", 10)):
        inv.stock_in(sku, qty)
    cart = {"tshirt": 2, "mug": 1}
    first = inv.reserve("ord-1", cart)
    print(f"ord-1 reserves 2 tshirt + 1 mug   -> {first.reservation_id}, available {inv.snapshot()}")
    print(f"ord-1 retried (client timeout)    -> {inv.reserve('ord-1', cart).reservation_id} again, not a second hold")
    try:
        inv.reserve("ord-2", {"mug": 2})
    except ConflictError as exc:
        print(f"ord-2 wants 2 mugs, 1 is free     -> rejected: {exc} (all or nothing)")

    seen = inv.versions("poster")
    inv.stock_in("poster", 5)  # a warehouse receipt lands between the read and the write
    try:
        inv.reserve("ord-2", {"poster": 1}, expected_versions=seen)
    except ConflictError as exc:
        print(f"ord-2 reserves on a stale read    -> rejected: {exc}")
    clock.advance(901)
    other = inv.reserve("ord-3", {"tshirt": 2, "mug": 1})
    print(f"900 s pass, ord-3 takes the units -> {other.reservation_id}; ord-1 expired lazily")
    try:
        inv.commit(first.reservation_id)
    except InvalidStateError as exc:
        print(f"ord-1 pays late, commit           -> rejected: {exc}: refund and re-offer")
    inv.release(other.reservation_id)  # a saga compensation gives the units back
    retried = inv.reserve("ord-3", {"tshirt": 2, "mug": 1})
    print(f"saga compensated ord-3, retried   -> {retried.reservation_id}, a fresh hold, not the released {other.reservation_id}")

    outbox = Outbox()

    def charge(order_id: str, amount: Money) -> str:
        if order_id == "ord-9":
            raise StepFailed(f"card declined for {amount}")
        return f"pay-{order_id}"

    saga = build_checkout_saga(inv, outbox, charge, lambda ref: outbox.append("refunded", ref=ref))
    good = {"order_id": "ord-4", "lines": {"poster": 3}, "amount": Money.of("29.97")}
    print(f"checkout ord-4 (3 posters)        -> saga {saga.start('ord-4', good)}, posters left {inv.available('poster')}")
    print(f"  outbox relayed                  -> {[e.topic for e in outbox.relay()]}")
    bad = {"order_id": "ord-9", "lines": {"poster": 4}, "amount": Money.of("39.96")}
    print(f"checkout ord-9, card declined     -> saga {saga.start('ord-9', bad)}, posters back to {inv.available('poster')}")
    print(f"  outbox relayed                  -> {[e.topic for e in outbox.relay()]}")

    drop = FlashSaleCounter("console", total=100, shards=8)
    claimed = [drop.take() for _ in range(100)]
    print(f"flash sale: 100 units, 8 shards   -> {len([c for c in claimed if c is not None])} claimed across shards {sorted(set(claimed))}")
    print(f"the 101st buyer                   -> take() returns {drop.take()}, remaining {drop.remaining()}")


if __name__ == "__main__":
    main()
