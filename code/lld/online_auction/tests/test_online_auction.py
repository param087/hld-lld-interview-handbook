import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, FakeClock, Money, SequentialIdGenerator, ValidationError
from lld.online_auction.models import (
    AuctionClosedError,
    AuctionStatus,
    BidTooLowError,
    Item,
    NotCancellableError,
)
from lld.online_auction.services import (
    AuctionHouse,
    AuctionService,
    BidService,
    CloseScheduler,
    NotificationService,
)
from lld.online_auction.store import AuctionStore
from lld.online_auction.strategies import (
    AntiSnipeExtension,
    AuctionRules,
    FixedIncrement,
    HardClose,
    PercentIncrement,
    TieredIncrement,
)

HOUR = 3600.0
ITEM = Item("i-1", "Leica M6", "35mm rangefinder")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000)


class Rig:
    """Store, services and mediator wired the way ``main`` would do it."""

    def __init__(self, clock: FakeClock, rules: AuctionRules | None = None, gateway: object | None = None) -> None:
        self.store = AuctionStore()
        self.rules = rules or AuctionRules.default()
        self.notifications = NotificationService()
        self.auctions = AuctionService(self.store, gateway, clock=clock, ids=SequentialIdGenerator("A"))  # type: ignore[arg-type]
        self.bids = BidService(self.store, self.rules, clock=clock, ids=SequentialIdGenerator("B"))
        self.house = AuctionHouse(
            self.store, self.auctions, self.bids, CloseScheduler(self.store), [self.notifications], clock=clock
        )
        self.clock = clock

    def open_listing(self, start: str = "100.00", reserve: str = "250.00", duration: float = HOUR) -> str:
        now = self.clock.now()
        auction = self.auctions.list_item(
            "seller", ITEM, Money.of(start), Money.of(reserve), now, now + duration
        )
        self.auctions.open_auction(auction.id)
        return auction.id


def test_the_first_bid_pays_the_start_price_and_a_proxy_pays_one_increment_over_the_runner_up(clock: FakeClock) -> None:
    rig = Rig(clock)
    auction_id = rig.open_listing()

    alice = rig.house.place_bid(auction_id, "alice", Money.of("150.00"))
    assert (alice.leader_id, alice.current_price) == ("alice", Money.of("100.00"))

    bob = rig.house.place_bid(auction_id, "bob", Money.of("200.00"))
    # bob's proxy beats alice's 150.00 maximum by one 2.50 band step, not by 50.00
    assert (bob.leader_id, bob.current_price) == ("bob", Money.of("152.50"))
    assert [b.automatic for b in rig.house.history(auction_id)] == [False, False]

    back = rig.house.place_bid(auction_id, "alice", Money.of("300.00"))
    assert (back.leader_id, back.current_price) == ("alice", Money.of("202.50"))
    assert rig.notifications.inbox("bob")[-1].message.startswith("outbid")


def test_a_tie_on_maximum_goes_to_whoever_committed_first(clock: FakeClock) -> None:
    rig = Rig(clock)
    auction_id = rig.open_listing()
    rig.house.place_bid(auction_id, "alice", Money.of("200.00"))
    state = rig.house.place_bid(auction_id, "bob", Money.of("200.00"))
    assert state.leader_id == "alice" and state.current_price == Money.of("200.00")


@pytest.mark.parametrize(
    ("bidder", "amount", "error"),
    [
        ("bob", "99.99", BidTooLowError),
        ("seller", "500.00", ValidationError),
    ],
)
def test_invalid_bids_are_refused_before_anything_changes(
    clock: FakeClock, bidder: str, amount: str, error: type[Exception]
) -> None:
    rig = Rig(clock)
    auction_id = rig.open_listing()
    with pytest.raises(error):
        rig.house.place_bid(auction_id, bidder, Money.of(amount))
    auction = rig.store.get(auction_id)
    assert auction.bid_count == 0 and auction.leader_id is None and auction.version == 1


# --8<-- [start:snipe]
def test_anti_snipe_extends_the_close_but_only_a_bounded_number_of_times(clock: FakeClock) -> None:
    rules = AuctionRules(TieredIncrement(), AntiSnipeExtension(window_seconds=60, extension_seconds=60, max_extensions=2))
    rig = Rig(clock, rules)
    auction_id = rig.open_listing(start="100.00", reserve="100.00", duration=300)
    rig.house.place_bid(auction_id, "alice", Money.of("120.00"))

    for round_number in range(4):  # four snipes, but only two extensions are allowed
        clock.set(rig.store.get(auction_id).ends_at - 10)
        rig.house.place_bid(auction_id, "bob", Money.of(f"{200 + round_number * 50}.00"))

    auction = rig.store.get(auction_id)
    assert auction.extension_count == 2  # the bound is what makes the auction terminate
    clock.set(auction.ends_at + 1)
    assert rig.house.tick()[0].status is AuctionStatus.SOLD


# --8<-- [end:snipe]


def test_a_bid_at_the_closing_instant_is_refused_and_the_tick_settles_the_auction(clock: FakeClock) -> None:
    rig = Rig(clock, AuctionRules(TieredIncrement(), HardClose()))
    auction_id = rig.open_listing(start="100.00", reserve="100.00")
    rig.house.place_bid(auction_id, "alice", Money.of("400.00"))

    clock.set(rig.store.get(auction_id).ends_at)  # exactly the closing instant
    with pytest.raises(AuctionClosedError, match="closed at"):
        rig.house.place_bid(auction_id, "bob", Money.of("500.00"))

    sold = rig.house.tick()[0]
    assert sold.status is AuctionStatus.SOLD and sold.winner_id == "alice"
    assert sold.final_price == Money.of("100.00") and rig.house.tick() == []
    with pytest.raises(AuctionClosedError):
        rig.house.place_bid(auction_id, "bob", Money.of("900.00"))


def test_reserve_not_met_and_a_failed_payment_both_end_unsold(clock: FakeClock) -> None:
    rig = Rig(clock)
    low = rig.open_listing(start="100.00", reserve="900.00")
    rig.house.place_bid(low, "alice", Money.of("150.00"))
    clock.advance(HOUR + 1)
    assert rig.house.tick()[0].status is AuctionStatus.UNSOLD

    class Declines:
        def charge(self, bidder_id: str, amount: Money) -> bool:
            return False

    broke = Rig(FakeClock(start=2_000_000), gateway=Declines())
    auction_id = broke.open_listing(start="100.00", reserve="100.00")
    broke.house.place_bid(auction_id, "alice", Money.of("500.00"))
    broke.clock.advance(HOUR + 1)
    unsold = broke.house.tick()[0]
    assert unsold.status is AuctionStatus.UNSOLD and unsold.winner_id is None


def test_a_seller_can_withdraw_only_before_the_first_bid(clock: FakeClock) -> None:
    rig = Rig(clock)
    empty = rig.open_listing()
    assert rig.house.cancel(empty, "seller").status is AuctionStatus.CANCELLED

    busy = rig.open_listing()
    rig.house.place_bid(busy, "alice", Money.of("150.00"))
    with pytest.raises(NotCancellableError):
        rig.house.cancel(busy, "seller")
    with pytest.raises(ValidationError):
        rig.house.cancel(busy, "someone-else")


def test_a_stale_version_is_rejected_by_the_compare_and_set(clock: FakeClock) -> None:
    rig = Rig(clock)
    auction_id = rig.open_listing()
    stale = rig.store.get(auction_id)  # version 1, captured before the bid
    rig.house.place_bid(auction_id, "alice", Money.of("150.00"))
    stale.current_price = Money.of("999.00")
    with pytest.raises(ConflictError, match="moved from version"):
        rig.store.commit(stale, stale.version)
    assert rig.store.get(auction_id).current_price == Money.of("100.00")


# --8<-- [start:concurrency]
def test_concurrent_bids_leave_one_leader_and_a_price_that_only_goes_up(clock: FakeClock) -> None:
    rig = Rig(clock, AuctionRules(TieredIncrement(), HardClose()))
    auction_id = rig.open_listing(start="100.00", reserve="100.00")
    accepted: dict[str, Money] = {}
    guard = threading.Lock()

    def bid(i: int) -> None:
        maximum = Money.of(f"{200 + i * 10}.00")
        try:
            rig.house.place_bid(auction_id, f"bidder-{i:02d}", maximum)
        except BidTooLowError:
            return  # the price ran ahead while this thread was queued: a legitimate refusal
        with guard:
            accepted[f"bidder-{i:02d}"] = maximum

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(bid, range(30)))

    auction = rig.store.get(auction_id)
    prices = [b.amount.cents for b in rig.house.history(auction_id)]
    assert auction.leader_id == max(accepted, key=lambda b: accepted[b].cents)  # highest maximum wins
    assert auction.current_price <= accepted[auction.leader_id]  # never pays above its own ceiling
    assert prices == sorted(prices) and auction.bid_count == len(accepted)


# --8<-- [end:concurrency]


@pytest.mark.parametrize(
    ("strategy", "current", "expected"),
    [
        (FixedIncrement(Money.of("1.00")), "10.00", "11.00"),
        (TieredIncrement(), "0.50", "0.55"),
        (TieredIncrement(), "150.00", "152.50"),
        (TieredIncrement(), "900.00", "910.00"),
        (PercentIncrement(500, Money.of("1.00")), "200.00", "210.00"),
        (PercentIncrement(500, Money.of("1.00")), "10.00", "11.00"),
    ],
)
def test_increment_strategies(strategy: object, current: str, expected: str) -> None:
    assert strategy.minimum_next(Money.of(current)) == Money.of(expected)  # type: ignore[attr-defined]
