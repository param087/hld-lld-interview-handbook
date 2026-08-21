"""Repository plus the per-auction lock, and the compare-and-set that guards writes.

The lock serialises the read-modify-write of one auction. ``commit`` additionally
checks the version it was handed: under the lock that check can never fail, which
makes it a cheap assertion that the locking discipline is intact -- and it is the
exact mechanism you would keep if you dropped the lock for an optimistic retry
against a shared database.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from common import ConflictError
from lld.online_auction.models import Auction, AutoBid, Bid, UnknownEntityError


# --8<-- [start:store]
class AuctionStore:
    """Auctions, their bid history and one proxy record per bidder per auction."""

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._auctions: dict[str, Auction] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._bids: dict[str, list[Bid]] = {}
        self._proxies: dict[str, dict[str, AutoBid]] = {}

    def add(self, auction: Auction) -> Auction:
        with self._registry_lock:
            self._auctions[auction.id] = auction
            self._locks[auction.id] = threading.RLock()
            self._bids[auction.id] = []
            self._proxies[auction.id] = {}
        return auction

    def get(self, auction_id: str) -> Auction:
        """A private copy. Mutate it, then hand it back to ``commit``."""
        with self._registry_lock:
            try:
                return self._auctions[auction_id].copy()
            except KeyError:
                raise UnknownEntityError(f"unknown auction {auction_id}") from None

    def proxies(self, auction_id: str) -> dict[str, AutoBid]:
        with self._registry_lock:
            return {bidder: proxy.copy() for bidder, proxy in self._proxies[auction_id].items()}

    def bids(self, auction_id: str) -> list[Bid]:
        with self._registry_lock:
            return list(self._bids[auction_id])

    def open_auction_ids(self) -> list[str]:
        with self._registry_lock:
            return sorted(self._auctions)

    @contextmanager
    def locked(self, auction_id: str) -> Iterator[None]:
        """The one lock that matters. Everything that touches an auction holds it."""
        with self._registry_lock:
            try:
                lock = self._locks[auction_id]
            except KeyError:
                raise UnknownEntityError(f"unknown auction {auction_id}") from None
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def commit(
        self,
        auction: Auction,
        expected_version: int,
        proxies: dict[str, AutoBid] | None = None,
        bid: Bid | None = None,
    ) -> Auction:
        """Compare-and-set on the version, then publish auction, proxies and history."""
        with self._registry_lock:
            stored = self._auctions.get(auction.id)
            if stored is None:
                raise UnknownEntityError(f"unknown auction {auction.id}")
            if stored.version != expected_version:
                raise ConflictError(
                    f"auction {auction.id} moved from version {expected_version} to {stored.version}"
                )
            auction.version = expected_version + 1
            self._auctions[auction.id] = auction
            if proxies is not None:
                self._proxies[auction.id] = proxies
            if bid is not None:
                self._bids[auction.id].append(bid)
            return auction.copy()


# --8<-- [end:store]
