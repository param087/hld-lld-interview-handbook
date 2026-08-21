from concurrent.futures import ThreadPoolExecutor

import pytest

from common import (
    ConflictError,
    FakeClock,
    InvalidStateError,
    NotFoundError,
    SequentialIdGenerator,
    ValidationError,
)
from hld.seat_hold import HoldStatus, RoomTypeInventory, SeatInventory, SeatStatus, WaitingRoom


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000.0)


def make_inventory(clock: FakeClock, seats: int = 4, **kwargs: float) -> SeatInventory:
    return SeatInventory("ev-1", [f"A{i}" for i in range(1, seats + 1)], clock, hold_ttl=600, grace=30, **kwargs)


def test_hold_is_all_or_nothing_and_bumps_versions(clock: FakeClock) -> None:
    inv = make_inventory(clock)
    hold = inv.hold("ann", ["A1", "A2"])
    assert hold.versions == {"A1": 1, "A2": 1}
    assert inv.seat_map()["A1"] is SeatStatus.HELD
    with pytest.raises(ConflictError, match="A2"):
        inv.hold("bob", ["A2", "A3"])
    assert inv.seat_map()["A3"] is SeatStatus.AVAILABLE  # nothing of the failed hold stuck
    booking = inv.confirm(hold.hold_id, "pay-1")
    assert booking.seat_ids == ("A1", "A2")
    assert inv.seat_map()["A1"] is SeatStatus.BOOKED
    assert inv.confirm(hold.hold_id, "pay-1") == booking  # retried webhook: same booking


def test_expired_hold_is_takeable_and_late_confirm_is_version_checked(clock: FakeClock) -> None:
    inv = make_inventory(clock)
    first = inv.hold("ann", ["A1"])
    clock.advance(601)
    assert inv.seat_map()["A1"] is SeatStatus.AVAILABLE  # shown as free after the TTL
    second = inv.hold("bob", ["A1"])  # lazy takeover, no sweeper needed
    assert second.versions["A1"] == 2
    with pytest.raises(ConflictError, match="re-assigned"):
        inv.confirm(first.hold_id, "pay-late")  # ann's payment must be refunded
    assert inv.confirm(second.hold_id, "pay-2").user_id == "bob"


def test_late_confirm_inside_grace_wins_when_nobody_moved(clock: FakeClock) -> None:
    inv = make_inventory(clock)
    hold = inv.hold("ann", ["A1"])
    clock.advance(620)  # 20 s past the TTL, inside the 30 s grace
    assert inv.confirm(hold.hold_id, "pay-1").seat_ids == ("A1",)
    other = inv.hold("bob", ["A2"])
    clock.advance(700)  # well past TTL + grace
    with pytest.raises(ConflictError, match="expired"):
        inv.confirm(other.hold_id, "pay-2")
    assert inv.seat_map()["A2"] is SeatStatus.AVAILABLE


def test_sweeper_releases_only_holds_past_grace(clock: FakeClock) -> None:
    inv = make_inventory(clock)
    old = inv.hold("ann", ["A1"])
    clock.advance(500)
    fresh = inv.hold("bob", ["A2"])
    clock.advance(131)  # old: 631 s (past TTL + grace); fresh: 131 s
    assert inv.expire_holds() == 1
    assert inv.seat_map() == {"A1": SeatStatus.AVAILABLE, "A2": SeatStatus.HELD, "A3": SeatStatus.AVAILABLE, "A4": SeatStatus.AVAILABLE}
    assert old.status is HoldStatus.EXPIRED
    with pytest.raises(ConflictError, match="expired"):
        inv.confirm(old.hold_id, "pay-1")  # the sweeper got there first: refund
    inv.release(fresh.hold_id)
    inv.release(fresh.hold_id)  # idempotent
    assert inv.available_count() == 4
    booked = inv.hold("cat", ["A3"])
    inv.confirm(booked.hold_id, "pay-3")
    with pytest.raises(InvalidStateError):
        inv.release(booked.hold_id)


def test_validation_and_not_found(clock: FakeClock) -> None:
    inv = make_inventory(clock)
    with pytest.raises(ValidationError):
        inv.hold("ann", [])
    with pytest.raises(ValidationError):
        inv.hold("ann", ["A1", "A1"])
    with pytest.raises(NotFoundError):
        inv.hold("ann", ["Z9"])
    with pytest.raises(NotFoundError):
        inv.confirm("nope", "pay")
    with pytest.raises(ValidationError):
        SeatInventory("ev", [], clock)


def test_waiting_room_is_fifo_and_tokens_expire(clock: FakeClock) -> None:
    room = WaitingRoom(clock, SequentialIdGenerator("tok"), token_ttl=120)
    inv = make_inventory(clock, waiting_room=room)
    assert [room.join(u) for u in ("ann", "bob", "cat")] == [1, 2, 3]
    assert room.join("bob") == 2  # rejoining keeps the place
    with pytest.raises(ConflictError, match="not admitted"):
        inv.hold("ann", ["A1"])
    admitted = room.admit(2)
    assert [t.user_id for t in admitted] == ["ann", "bob"]
    assert room.waiting() == 1 and room.join("cat") == 1
    with pytest.raises(ConflictError, match="not admitted"):
        inv.hold("ann", ["A1"], admitted[1].token)  # someone else's token
    inv.hold("ann", ["A1"], admitted[0].token)
    clock.advance(121)
    with pytest.raises(ConflictError, match="expired"):
        inv.hold("bob", ["A2"], admitted[1].token)


def test_concurrent_holds_on_one_seat_have_exactly_one_winner(clock: FakeClock) -> None:
    inv = make_inventory(clock, seats=50)

    def try_hold(i: int) -> bool:
        try:
            inv.hold(f"user-{i}", ["A1", f"A{2 + i % 49}"])
            return True
        except ConflictError:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(try_hold, range(200)))
    assert results.count(True) == 1
    held = [s for s, status in inv.seat_map().items() if status is SeatStatus.HELD]
    assert len(held) == 2 and "A1" in held  # the loser's second seat was never touched


def test_hotel_stay_is_all_or_nothing_across_nights_with_overbooking() -> None:
    hotel = RoomTypeInventory("deluxe", rooms=2, overbook_pct=50)
    assert hotel.allotment == 3
    first = hotel.reserve("g1", check_in=10, nights=3)
    hotel.reserve("g2", check_in=11, nights=3)
    hotel.reserve("g3", check_in=12, nights=3)
    with pytest.raises(ConflictError, match=r"nights \[12\]"):
        hotel.reserve("g4", check_in=11, nights=2)
    assert hotel.sold(11) == 2  # the rejected stay touched nothing
    hotel.cancel(first)
    hotel.cancel(first)  # idempotent
    hotel.reserve("g4", check_in=11, nights=2)
    assert (hotel.sold(10), hotel.sold(11), hotel.sold(12)) == (0, 2, 3)
    with pytest.raises(ValidationError):
        hotel.reserve("g5", check_in=1, nights=0)


def test_concurrent_hotel_reservations_never_exceed_allotment() -> None:
    hotel = RoomTypeInventory("std", rooms=10)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda i: _try_reserve(hotel, i), range(100)))
    assert outcomes.count(True) == 10
    assert all(hotel.sold(n) <= 10 for n in range(0, 12))


def _try_reserve(hotel: RoomTypeInventory, i: int) -> bool:
    try:
        hotel.reserve(f"g{i}", check_in=i % 3, nights=5)
        return True
    except ConflictError:
        return False


def test_hold_status_transitions(clock: FakeClock) -> None:
    inv = make_inventory(clock)
    hold = inv.hold("ann", ["A1"])
    assert hold.status is HoldStatus.ACTIVE
    inv.release(hold.hold_id)
    assert hold.status is HoldStatus.RELEASED
    with pytest.raises(InvalidStateError, match="released"):
        inv.confirm(hold.hold_id, "pay")  # paying for a cancelled hold is a client bug, not a race
    again = inv.hold("ann", ["A1"])
    inv.confirm(again.hold_id, "pay")
    assert again.status is HoldStatus.CONFIRMED
