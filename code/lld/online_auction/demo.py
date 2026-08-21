"""One listing: four proxy bidders, an outbid notice, an anti-snipe extension, a sale."""

from common import FakeClock, Money, SequentialIdGenerator
from lld.online_auction.models import BidTooLowError, Item, NotCancellableError
from lld.online_auction.services import (
    AuctionHouse,
    AuctionService,
    BidService,
    BidValidator,
    CloseScheduler,
    NotificationService,
)
from lld.online_auction.store import AuctionStore
from lld.online_auction.strategies import AuctionRules

HOUR = 3600.0


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    store = AuctionStore()
    rules = AuctionRules.default()
    notifications = NotificationService()
    auctions = AuctionService(store, clock=clock, ids=SequentialIdGenerator("A"))
    bids = BidService(store, rules, clock=clock, ids=SequentialIdGenerator("B"))
    house = AuctionHouse(store, auctions, bids, CloseScheduler(store), [notifications], clock=clock)
    validator = BidValidator(rules)

    item = Item("i-1", "Leica M6", "35mm rangefinder, 1987")
    auction = auctions.list_item("seller", item, Money.of("100.00"), Money.of("250.00"), clock.now(), clock.now() + HOUR)
    auctions.open_auction(auction.id)
    for watcher in ("alice", "bob", "carol"):
        notifications.watch(auction.id, watcher)
    print(f"{auction.id} {item.title}: start {auction.start_price}, reserve {auction.reserve_price}, minimum bid {validator.minimum_next(auction)}")

    for bidder, maximum in (("alice", "150.00"), ("bob", "200.00"), ("alice", "300.00"), ("carol", "260.00")):
        clock.advance(60)
        state = house.place_bid(auction.id, bidder, Money.of(maximum))
        print(f"{bidder} sets a maximum of {maximum} -> {state.leader_id} leads at {state.current_price}")
    print(f"reserve met: {store.get(auction.id).reserve_met()}, bob's inbox: {[e.kind.value for e in notifications.inbox('bob')]}")

    try:
        house.place_bid(auction.id, "dave", Money.of("261.00"))
    except BidTooLowError as exc:
        print(f"dave bids too little: {exc}")
    try:
        house.cancel(auction.id, "seller")
    except NotCancellableError as exc:
        print(f"seller cannot withdraw: {exc}")

    clock.set(store.get(auction.id).ends_at - 30)  # 30 s left: sniping territory
    state = house.place_bid(auction.id, "carol", Money.of("400.00"))
    print(f"carol snipes at 400.00 -> {state.leader_id} leads at {state.current_price}, extended {state.extension_count} time(s)")
    print(f"alice was told: {[e.message for e in notifications.inbox('alice') if e.kind.value == 'outbid']}")

    clock.set(state.ends_at - 1)
    print(f"one second left: tick closes {len(house.tick())} auction(s)")
    clock.advance(2)
    closed = house.tick()[0]
    print(f"{closed.status}: winner {closed.winner_id} at {closed.final_price} after {closed.bid_count} bids")
    print(f"history: {[f'{b.bidder_id} {b.amount}' for b in house.history(auction.id)]}")


if __name__ == "__main__":
    main()
