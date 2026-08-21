"""Enums, errors and entities for the auction house.

The important modelling decision is in ``AutoBid``: *every* bid is a maximum.
A manual bid of 150.00 is a proxy whose ceiling happens to be 150.00, which
collapses the two kinds of bidding into one data structure and one rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError


# --8<-- [start:enums]
class AuctionStatus(StrEnum):
    SCHEDULED = "scheduled"  # created, not yet open for bids
    OPEN = "open"  # accepting bids
    CLOSED = "closed"  # the clock ran out, settlement not finished
    SOLD = "sold"  # reserve met and payment taken
    UNSOLD = "unsold"  # reserve not met, or the winner's payment failed
    CANCELLED = "cancelled"  # pulled by the seller before the first bid


class EventKind(StrEnum):
    BID_PLACED = "bid_placed"
    OUTBID = "outbid"
    EXTENDED = "extended"
    CLOSED = "closed"
    SOLD = "sold"
    UNSOLD = "unsold"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({AuctionStatus.SOLD, AuctionStatus.UNSOLD, AuctionStatus.CANCELLED})

AUCTION_TRANSITIONS: Mapping[AuctionStatus, frozenset[AuctionStatus]] = {
    AuctionStatus.SCHEDULED: frozenset({AuctionStatus.OPEN, AuctionStatus.CANCELLED}),
    AuctionStatus.OPEN: frozenset({AuctionStatus.CLOSED, AuctionStatus.CANCELLED}),
    AuctionStatus.CLOSED: frozenset({AuctionStatus.SOLD, AuctionStatus.UNSOLD}),
    AuctionStatus.SOLD: frozenset(),
    AuctionStatus.UNSOLD: frozenset(),
    AuctionStatus.CANCELLED: frozenset(),
}


# --8<-- [end:enums]


# --8<-- [start:errors]
class BidTooLowError(ValidationError):
    """The offered maximum does not reach the minimum next bid."""


class AuctionClosedError(InvalidStateError):
    """The auction is not open, or the bid arrived after the closing instant."""


class NotCancellableError(ConflictError):
    """A seller cannot pull an auction once somebody has bid on it."""


class UnknownEntityError(NotFoundError):
    """No such auction, bidder or bid."""


# --8<-- [end:errors]


# --8<-- [start:entities]
@dataclass(frozen=True, slots=True)
class Seller:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Bidder:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class Bid:
    """A row in the history: who was leading, at what price, at what moment."""

    id: str
    auction_id: str
    bidder_id: str
    amount: Money
    at: float
    automatic: bool  # True when a proxy raised the price without the bidder acting


@dataclass(slots=True)
class AutoBid:
    """One bidder's standing instruction: never pay more than ``maximum``.

    ``sequence`` is the arrival order and is the tie-break: when two bidders
    share the same maximum, the one who committed to it first wins.
    """

    auction_id: str
    bidder_id: str
    maximum: Money
    sequence: int
    at: float

    def raise_to(self, maximum: Money, at: float) -> None:
        if maximum <= self.maximum:
            raise BidTooLowError(f"{self.bidder_id} already committed up to {self.maximum}")
        self.maximum = maximum
        self.at = at

    def copy(self) -> AutoBid:
        return AutoBid(self.auction_id, self.bidder_id, self.maximum, self.sequence, self.at)


@dataclass(slots=True)
class Auction:
    """The aggregate. ``version`` changes on every accepted bid: it is the CAS token."""

    id: str
    item: Item
    seller_id: str
    start_price: Money
    reserve_price: Money
    starts_at: float
    ends_at: float
    status: AuctionStatus = AuctionStatus.SCHEDULED
    current_price: Money = Money(0)
    leader_id: str | None = None
    bid_count: int = 0
    extension_count: int = 0
    winner_id: str | None = None
    final_price: Money | None = None
    version: int = 0

    def transition_to(self, status: AuctionStatus) -> None:
        if status not in AUCTION_TRANSITIONS[self.status]:
            raise AuctionClosedError(f"auction {self.id}: {self.status} cannot become {status}")
        self.status = status

    def is_live(self, now: float) -> bool:
        return self.status is AuctionStatus.OPEN and self.starts_at <= now < self.ends_at

    def reserve_met(self) -> bool:
        return self.current_price >= self.reserve_price

    def copy(self) -> Auction:
        return Auction(
            self.id, self.item, self.seller_id, self.start_price, self.reserve_price, self.starts_at,
            self.ends_at, self.status, self.current_price, self.leader_id, self.bid_count,
            self.extension_count, self.winner_id, self.final_price, self.version,
        )


@dataclass(frozen=True, slots=True)
class AuctionEvent:
    """What listeners receive. Frozen, so a watcher cannot rewrite the record."""

    kind: EventKind
    auction_id: str
    actor_id: str
    message: str
    at: float


# --8<-- [end:entities]
