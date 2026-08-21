"""Stock: reserve, commit, release, expire -- and the locks that stop an oversell."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock, ValidationError
from lld.ecommerce_order_inventory.models import (
    HoldExpiredError,
    HoldLine,
    HoldStatus,
    InventoryItem,
    OutOfStockError,
    StockHold,
    UnknownSkuError,
    Warehouse,
)


# --8<-- [start:inventory]
class InventoryService:
    """The only object allowed to move a unit between available and reserved.

    **Lock granularity.** One lock per SKU, created lazily. Not one global lock
    (every buyer in the shop would queue behind the one popular item) and not one
    lock per warehouse row (a two-SKU basket would then take an unbounded number
    of locks in an order nobody has reasoned about). A basket spanning several
    SKUs acquires their locks **in sorted id order**, so two baskets containing
    the same pair of SKUs in opposite order still cannot deadlock.

    **Why all-or-nothing works.** ``reserve`` holds every lock it needs for the
    whole check-then-write. Either it finds enough of everything and writes all
    the rows, or it has written nothing at all and raises. There is no window in
    which a partly-reserved basket exists, so there is no compensation to run.

    **The TTL.** A hold is a promise with a deadline. ``expire_holds`` puts the
    units of abandoned checkouts back on the shelf, which is what stops a
    flash-sale bot from parking your whole stock in a cart for an hour.
    """

    HOLD_TTL_SECONDS = 900.0  # 15 minutes to finish a checkout

    def __init__(
        self,
        warehouses: Sequence[Warehouse] = (),
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        hold_ttl: float = HOLD_TTL_SECONDS,
    ) -> None:
        self._warehouses = list(warehouses)
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("H")
        self._ttl = hold_ttl
        self._rows: dict[tuple[str, str], InventoryItem] = {}
        self._sku_locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()  # guards _sku_locks and _rows membership
        self._holds: dict[str, StockHold] = {}
        self._holds_lock = threading.Lock()

    # -- stock levels -------------------------------------------------------------
    def add_stock(self, sku_id: str, warehouse_id: str, quantity: int) -> InventoryItem:
        with self._locked([sku_id]):
            row = self._row(sku_id, warehouse_id, create=True)
            row.restock(quantity)
            return row

    def row(self, sku_id: str, warehouse_id: str) -> InventoryItem:
        with self._locked([sku_id]):
            return self._row(sku_id, warehouse_id)

    def available(self, sku_id: str) -> int:
        """Free units across every warehouse. A read, so it takes only that SKU's lock."""
        with self._locked([sku_id]):
            return sum(r.available for r in self._rows_for(sku_id))

    def reserved(self, sku_id: str) -> int:
        with self._locked([sku_id]):
            return sum(r.reserved for r in self._rows_for(sku_id))

    def on_hand(self, sku_id: str) -> int:
        """available + reserved. This number only changes when goods physically move."""
        with self._locked([sku_id]):
            return sum(r.available + r.reserved for r in self._rows_for(sku_id))

    # -- the reserve / commit / release cycle -------------------------------------
    def reserve(self, lines: dict[str, int], owner: str) -> StockHold:
        """All-or-nothing hold across SKUs and warehouses. The oversell guard."""
        if not lines:
            raise ValidationError("nothing to reserve")
        allocated: list[HoldLine] = []
        with self._locked(lines):
            for sku_id in sorted(lines):
                allocated.extend(self._plan(sku_id, lines[sku_id]))  # raises before any write
            for line in allocated:
                self._rows[(line.sku_id, line.warehouse_id)].hold(line.quantity)
        now = self._clock.now()
        hold = StockHold(self._ids.next_id(), owner, tuple(allocated), now, now + self._ttl)
        with self._holds_lock:
            self._holds[hold.id] = hold
        return hold

    def commit(self, hold_id: str) -> StockHold:
        """The parcel is packed: the units leave both counters for good.

        This is the one operation the deadline blocks. A checkout that comes back
        after its TTL must not take units the shop has already re-offered.
        """
        return self._settle(hold_id, HoldStatus.COMMITTED)

    def release(self, hold_id: str) -> StockHold:
        """Payment failed or the order was cancelled: the units go back on the shelf.

        Giving units back is always safe, so an overdue hold can still be released
        -- only committing it is refused.
        """
        return self._settle(hold_id, HoldStatus.RELEASED)

    def expire_holds(self) -> list[StockHold]:
        """Sweep abandoned checkouts. Run it from a timer, or before a flash sale."""
        now = self._clock.now()
        with self._holds_lock:
            stale = [h for h in self._holds.values() if h.status is HoldStatus.HELD and now >= h.expires_at]
        expired: list[StockHold] = []
        for stale_hold in stale:
            try:
                expired.append(self._settle(stale_hold.id, HoldStatus.EXPIRED))
            except HoldExpiredError:
                continue  # a checkout committed or released it while we were sweeping
        return expired

    def hold(self, hold_id: str) -> StockHold:
        with self._holds_lock:
            try:
                return self._holds[hold_id]
            except KeyError:
                raise UnknownSkuError(f"no hold {hold_id}") from None

    def low_stock(self, threshold: int) -> list[tuple[str, int]]:
        """SKUs at or below the reorder point, for the restock alert."""
        with self._registry_lock:
            sku_ids = {sku_id for sku_id, _ in self._rows}
        low = [(sku_id, self.available(sku_id)) for sku_id in sorted(sku_ids)]
        return [(sku_id, free) for sku_id, free in low if free <= threshold]

    # -- internals ----------------------------------------------------------------
    def _settle(self, hold_id: str, target: HoldStatus) -> StockHold:
        """Check-and-flip the hold, then move the counters. One winner per hold."""
        hold = self.hold(hold_id)
        with self._holds_lock:
            if hold.status is not HoldStatus.HELD:
                raise HoldExpiredError(f"hold {hold_id} is already {hold.status}")
            if target is HoldStatus.COMMITTED and not hold.is_live(self._clock.now()):
                raise HoldExpiredError(f"hold {hold_id} expired at {hold.expires_at}")
            hold.status = target
        with self._locked({line.sku_id for line in hold.lines}):
            for line in hold.lines:
                row = self._rows[(line.sku_id, line.warehouse_id)]
                if target is HoldStatus.COMMITTED:
                    row.commit(line.quantity)
                else:
                    row.release(line.quantity)
        return hold

    def _plan(self, sku_id: str, quantity: int) -> list[HoldLine]:
        """Split one SKU across warehouses. Raises before anything is written."""
        remaining = quantity
        plan: list[HoldLine] = []
        for row in self._rows_for(sku_id):
            if remaining == 0:
                break
            take = min(row.available, remaining)
            if take:
                plan.append(HoldLine(sku_id, row.warehouse_id, take))
                remaining -= take
        if remaining:
            raise OutOfStockError(f"{sku_id}: short by {remaining} of {quantity}")
        return plan

    def _rows_for(self, sku_id: str) -> list[InventoryItem]:
        with self._registry_lock:
            keys = [k for k in self._rows if k[0] == sku_id]
        return [self._rows[k] for k in sorted(keys)]

    def _row(self, sku_id: str, warehouse_id: str, create: bool = False) -> InventoryItem:
        key = (sku_id, warehouse_id)
        with self._registry_lock:
            row = self._rows.get(key)
            if row is None:
                if not create:
                    raise UnknownSkuError(f"no stock row for {sku_id} at {warehouse_id}")
                row = self._rows.setdefault(key, InventoryItem(sku_id, warehouse_id))
            return row

    def _lock_for(self, sku_id: str) -> threading.Lock:
        with self._registry_lock:
            return self._sku_locks.setdefault(sku_id, threading.Lock())

    @contextmanager
    def _locked(self, sku_ids: Iterable[str]) -> Iterator[None]:
        """Take every SKU lock in sorted id order, so lock cycles are impossible."""
        locks = [self._lock_for(sku_id) for sku_id in sorted(set(sku_ids))]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()


# --8<-- [end:inventory]
