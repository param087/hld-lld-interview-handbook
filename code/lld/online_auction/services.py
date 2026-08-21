"""Validation, bidding, closing, notification, and the mediator that wires them.

``BidService`` holds the algorithm worth remembering: proxy resolution is a sort,
not a loop. ``AuctionHouse`` is the Mediator -- the colleagues below never call
each other, so adding a listener or swapping the scheduler touches one class.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Protocol

from common import Clock, IdGenerator, Money, SequentialIdGenerator, SystemClock, ValidationError
from lld.online_auction.models import (
    Auction,
    AuctionClosedError,
    AuctionEvent,
    AuctionStatus,
    AutoBid,
    Bid,
    BidTooLowError,
    EventKind,
    Item,
    NotCancellableError,
)
from lld.online_auction.store import AuctionStore
from lld.online_auction.strategies import AuctionRules


# --8<-- [start:observer]
class AuctionListener(Protocol):
    """Observer interface: anything that reacts to what happens in an auction."""

    def on_event(self, event: AuctionEvent) -> None: ...


class NotificationService:
    """Fans events out to the people watching that auction, plus the actor named on it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watchers: dict[str, set[str]] = {}
        self._inbox: dict[str, list[AuctionEvent]] = {}

    def watch(self, auction_id: str, user_id: str) -> None:
        with self._lock:
            self._watchers.setdefault(auction_id, set()).add(user_id)

    def on_event(self, event: AuctionEvent) -> None:
        with self._lock:
            recipients = set(self._watchers.get(event.auction_id, ()))
            if event.kind is EventKind.OUTBID:
                recipients = {event.actor_id}  # only the person who lost the lead
            for user_id in recipients:
                self._inbox.setdefault(user_id, []).append(event)

    def inbox(self, user_id: str) -> list[AuctionEvent]:
        with self._lock:
            return list(self._inbox.get(user_id, ()))


class PaymentGateway(Protocol):
    """Charging the winner is someone else's system; the auction only needs a verdict."""

    def charge(self, bidder_id: str, amount: Money) -> bool: ...


class AlwaysApprovesGateway:
    def charge(self, bidder_id: str, amount: Money) -> bool:
        return True


# --8<-- [end:observer]


# --8<-- [start:validator]
class BidValidator:
    """Every reason a bid is refused, in one place, in the order it should be checked."""

    def __init__(self, rules: AuctionRules) -> None:
        self._rules = rules

    def minimum_next(self, auction: Auction) -> Money:
        """The first bid pays the start price; after that, one increment over the leader."""
        if auction.bid_count == 0:
            return auction.start_price
        return self._rules.increment.minimum_next(auction.current_price)

    def check(self, auction: Auction, bidder_id: str, maximum: Money, now: float) -> None:
        if auction.status is not AuctionStatus.OPEN:
            raise AuctionClosedError(f"auction {auction.id} is {auction.status}")
        if now < auction.starts_at:
            raise AuctionClosedError(f"auction {auction.id} opens later")
        if now >= auction.ends_at:
            raise AuctionClosedError(f"auction {auction.id} closed at {auction.ends_at}")
        if bidder_id == auction.seller_id:
            raise ValidationError("a seller cannot bid on their own auction")
        if maximum.currency != auction.start_price.currency:
            raise ValidationError("bid currency does not match the listing")
        floor = self.minimum_next(auction)
        if maximum < floor:
            raise BidTooLowError(f"bid at least {floor}, got {maximum}")


# --8<-- [end:validator]


# --8<-- [start:bidservice]
class BidService:
    """Places bids and resolves proxies.

    The resolution is deliberately not a loop. Every bidder is represented by a
    maximum, so the outcome is: the highest maximum leads, and pays one increment
    over the second-highest maximum, capped at its own maximum. That is one sort
    and two comparisons -- it always terminates, and two proxies cannot ping-pong.
    """

    def __init__(
        self,
        store: AuctionStore,
        rules: AuctionRules,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._store = store
        self._rules = rules
        self._validator = BidValidator(rules)
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("B")

    def place_bid(self, auction_id: str, bidder_id: str, maximum: Money) -> tuple[Auction, list[AuctionEvent]]:
        now = self._clock.now()
        with self._store.locked(auction_id):
            auction = self._store.get(auction_id)
            version = auction.version
            self._validator.check(auction, bidder_id, maximum, now)
            proxies = self._store.proxies(auction_id)
            self._record_maximum(proxies, auction_id, bidder_id, maximum, now)

            previous_leader = auction.leader_id
            leader_id, price = self._resolve(auction, proxies)
            auction.current_price = price
            auction.leader_id = leader_id
            auction.bid_count += 1
            bid = Bid(self._ids.next_id(), auction_id, leader_id, price, now, automatic=leader_id != bidder_id)

            events = [
                AuctionEvent(EventKind.BID_PLACED, auction_id, bidder_id, f"{leader_id} leads at {price}", now)
            ]
            new_end = self._rules.closing.next_end_time(auction, now)
            if new_end > auction.ends_at:
                auction.ends_at = new_end
                auction.extension_count += 1
                events.append(
                    AuctionEvent(EventKind.EXTENDED, auction_id, bidder_id, f"extended to {new_end}", now)
                )
            if previous_leader is not None and previous_leader != leader_id:
                events.append(
                    AuctionEvent(EventKind.OUTBID, auction_id, previous_leader, f"outbid at {price}", now)
                )
            return self._store.commit(auction, version, proxies, bid), events

    @staticmethod
    def _record_maximum(
        proxies: dict[str, AutoBid], auction_id: str, bidder_id: str, maximum: Money, now: float
    ) -> None:
        """A manual bid is a proxy whose ceiling is the amount typed in."""
        existing = proxies.get(bidder_id)
        if existing is None:
            proxies[bidder_id] = AutoBid(auction_id, bidder_id, maximum, len(proxies) + 1, now)
        else:
            existing.raise_to(maximum, now)  # refuses a maximum that is not an increase

    def _resolve(self, auction: Auction, proxies: dict[str, AutoBid]) -> tuple[str, Money]:
        """Highest maximum wins; ties go to whoever committed first."""
        ordered = sorted(proxies.values(), key=lambda p: (-p.maximum.cents, p.sequence))
        leader = ordered[0]
        if len(ordered) == 1:
            price = auction.start_price
        else:
            contested = self._rules.increment.minimum_next(ordered[1].maximum)
            price = min(leader.maximum, contested)
        floor = max(auction.current_price, auction.start_price)
        return leader.bidder_id, max(price, floor)


# --8<-- [end:bidservice]


# --8<-- [start:auctionservice]
class AuctionService:
    """Listing, opening, cancelling and settling. Never touches proxies or increments."""

    def __init__(
        self,
        store: AuctionStore,
        gateway: PaymentGateway | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._store = store
        self._gateway = gateway or AlwaysApprovesGateway()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("A")

    def list_item(
        self,
        seller_id: str,
        item: Item,
        start_price: Money,
        reserve_price: Money,
        starts_at: float,
        ends_at: float,
    ) -> Auction:
        if ends_at <= starts_at:
            raise ValidationError("an auction must end after it starts")
        if start_price.cents <= 0 or reserve_price < start_price:
            raise ValidationError("reserve price must be at least the start price")
        auction = Auction(
            self._ids.next_id(), item, seller_id, start_price, reserve_price, starts_at, ends_at,
            current_price=start_price,
        )
        return self._store.add(auction)

    def open_auction(self, auction_id: str) -> Auction:
        with self._store.locked(auction_id):
            auction = self._store.get(auction_id)
            auction.transition_to(AuctionStatus.OPEN)
            return self._store.commit(auction, auction.version)

    def cancel(self, auction_id: str, seller_id: str) -> tuple[Auction, AuctionEvent]:
        """Only before the first bid: once someone has bid, the seller is committed."""
        with self._store.locked(auction_id):
            auction = self._store.get(auction_id)
            if auction.seller_id != seller_id:
                raise ValidationError("only the seller can cancel a listing")
            if auction.bid_count > 0:
                raise NotCancellableError(f"auction {auction.id} already has {auction.bid_count} bids")
            auction.transition_to(AuctionStatus.CANCELLED)
            now = self._clock.now()
            event = AuctionEvent(EventKind.CANCELLED, auction_id, seller_id, "listing withdrawn", now)
            return self._store.commit(auction, auction.version), event

    def close(self, auction_id: str) -> tuple[Auction, list[AuctionEvent]]:
        """Stop the clock, then settle. The gateway is called between two transactions."""
        now = self._clock.now()
        events: list[AuctionEvent] = []
        with self._store.locked(auction_id):
            auction = self._store.get(auction_id)
            if auction.status is AuctionStatus.OPEN:
                auction.transition_to(AuctionStatus.CLOSED)
                auction = self._store.commit(auction, auction.version)
                events.append(AuctionEvent(EventKind.CLOSED, auction_id, auction.seller_id, "bidding ended", now))
            elif auction.status is not AuctionStatus.CLOSED:
                raise AuctionClosedError(f"auction {auction_id} is {auction.status}")

        won = auction.leader_id is not None and auction.reserve_met()
        paid = won and self._gateway.charge(auction.leader_id or "", auction.current_price)

        with self._store.locked(auction_id):
            settled = self._store.get(auction_id)
            version = settled.version
            if paid:
                settled.winner_id = settled.leader_id
                settled.final_price = settled.current_price
                settled.transition_to(AuctionStatus.SOLD)
                events.append(
                    AuctionEvent(EventKind.SOLD, auction_id, settled.winner_id or "", f"sold at {settled.current_price}", now)
                )
            else:
                reason = "reserve not met" if not won else "payment failed"
                settled.transition_to(AuctionStatus.UNSOLD)
                events.append(AuctionEvent(EventKind.UNSOLD, auction_id, settled.seller_id, reason, now))
            return self._store.commit(settled, version), events


class CloseScheduler:
    """Knows which auctions are due. Deliberately not a Singleton: it is injected.

    Driven by ``tick(now)`` rather than a background thread, so tests and the demo
    control time exactly. A production version replaces ``due`` with a timer wheel
    or a delayed queue; nothing else changes.
    """

    def __init__(self, store: AuctionStore) -> None:
        self._store = store

    def due(self, now: float) -> list[str]:
        """Open auctions whose clock has run out, plus any left mid-settlement by a crash."""
        due: list[str] = []
        for auction_id in self._store.open_auction_ids():
            auction = self._store.get(auction_id)
            expired = auction.status is AuctionStatus.OPEN and now >= auction.ends_at
            if expired or auction.status is AuctionStatus.CLOSED:
                due.append(auction_id)
        return due


# --8<-- [end:auctionservice]


# --8<-- [start:mediator]
class AuctionHouse:
    """Mediator: the one object the app talks to, and the only one that knows the others.

    ``BidService`` does not know notifications exist; ``CloseScheduler`` does not
    know how to close anything; ``NotificationService`` does not know what a bid
    is. Adding an anti-shill detector or a second listener touches this class only.
    """

    def __init__(
        self,
        store: AuctionStore,
        auctions: AuctionService,
        bids: BidService,
        scheduler: CloseScheduler,
        listeners: Iterable[AuctionListener] = (),
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._auctions = auctions
        self._bids = bids
        self._scheduler = scheduler
        self._listeners = list(listeners)
        self._clock = clock or SystemClock()

    def place_bid(self, auction_id: str, bidder_id: str, maximum: Money) -> Auction:
        auction, events = self._bids.place_bid(auction_id, bidder_id, maximum)
        self._publish(events)
        return auction

    def cancel(self, auction_id: str, seller_id: str) -> Auction:
        auction, event = self._auctions.cancel(auction_id, seller_id)
        self._publish([event])
        return auction

    def tick(self, now: float | None = None) -> list[Auction]:
        """Close everything whose clock has run out. Idempotent: nothing else is due after."""
        moment = self._clock.now() if now is None else now
        closed: list[Auction] = []
        for auction_id in self._scheduler.due(moment):
            auction, events = self._auctions.close(auction_id)
            self._publish(events)
            closed.append(auction)
        return closed

    def history(self, auction_id: str) -> list[Bid]:
        return self._store.bids(auction_id)

    def _publish(self, events: Iterable[AuctionEvent]) -> None:
        # Outside the auction lock: a slow listener must never block a bidder.
        for event in events:
            for listener in self._listeners:
                listener.on_event(event)


# --8<-- [end:mediator]
