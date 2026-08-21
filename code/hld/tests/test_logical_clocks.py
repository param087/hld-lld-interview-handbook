from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.logical_clocks import (
    HybridLogicalClock,
    HybridTimestamp,
    LamportClock,
    LastWriterWinsStore,
    Ordering,
    VectorClock,
    VersionedStore,
    concurrent,
    happens_before,
    lamport_total_order,
)


def test_lamport_orders_causal_events_and_says_nothing_about_concurrent_ones() -> None:
    a, b, c = LamportClock("A"), LamportClock("B"), LamportClock("C")
    a_write = a.tick()  # 1
    m1 = a.send()  # 2
    c_write = c.tick()  # 1, concurrent with everything on A
    b_recv = b.receive(m1)  # max(0, 2) + 1 = 3
    b_write = b.tick()  # 4
    assert (a_write, m1, b_recv, b_write) == (1, 2, 3, 4)
    assert a_write < m1 < b_recv < b_write  # a -> b implies L(a) < L(b)
    assert c_write < b_write  # ...but the converse does not hold: these two are concurrent
    assert c_write == 1 and c.node_id == "C"
    # The total order is consistent with causality, but it also orders the concurrent pair.
    assert lamport_total_order([b.stamp(), c.stamp(), a.stamp()]) == [(1, "C"), (2, "A"), (4, "B")]


def test_lamport_receive_jumps_past_the_stamp_and_never_goes_backwards() -> None:
    clock = LamportClock("A", start=5)
    assert clock.receive(2) == 6  # a stale message still counts as one local event
    assert clock.receive(99) == 100  # a message from the future drags the clock forward
    assert clock.time == 100
    with pytest.raises(ValidationError):
        clock.receive(-1)
    with pytest.raises(ValidationError):
        LamportClock("")
    with pytest.raises(ValidationError):
        LamportClock("A", start=-1)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ({"A": 1}, {"A": 2}, Ordering.BEFORE),
        ({"A": 2}, {"A": 1}, Ordering.AFTER),
        ({"A": 2, "B": 1}, {"A": 2, "B": 1}, Ordering.EQUAL),
        ({}, {}, Ordering.EQUAL),
        ({"A": 1}, {"B": 1}, Ordering.CONCURRENT),
        ({"A": 2, "B": 1}, {"A": 1, "B": 2}, Ordering.CONCURRENT),
        ({"A": 1}, {"A": 1, "B": 1}, Ordering.BEFORE),
    ],
)
def test_vector_clock_compare_covers_every_ordering(
    left: dict[str, int], right: dict[str, int], expected: Ordering
) -> None:
    a, b = VectorClock.of(left), VectorClock.of(right)
    assert a.compare(b) is expected
    assert happens_before(a, b) is (expected is Ordering.BEFORE)
    assert concurrent(a, b) is concurrent(b, a) is (expected is Ordering.CONCURRENT)
    assert a.dominates(b) is (expected in (Ordering.AFTER, Ordering.EQUAL))


def test_vector_clock_tick_and_merge_return_new_values() -> None:
    start = VectorClock.of({"A": 1})
    ticked = start.tick("B")
    assert start.as_dict() == {"A": 1}  # frozen: the original is untouched
    assert ticked.as_dict() == {"A": 1, "B": 1}
    merged = VectorClock.of({"A": 3, "C": 1}).merge(VectorClock.of({"A": 1, "B": 5}))
    assert merged.as_dict() == {"A": 3, "B": 5, "C": 1}  # pointwise maximum
    assert str(merged) == "{A:3, B:5, C:1}"
    assert VectorClock.of({"A": 0}) == VectorClock()  # zeros are not stored
    assert hash(ticked) == hash(VectorClock.of({"B": 1, "A": 1}))  # order-independent
    with pytest.raises(ValidationError):
        VectorClock.of({"A": -1})
    with pytest.raises(ValidationError):
        VectorClock.of({"": 1})


def test_versioned_store_keeps_concurrent_siblings_until_a_client_resolves_them() -> None:
    store = VersionedStore()
    socks = store.put("cart:7", "add socks", "A")
    shoes = store.put("cart:7", "add shoes", "B")  # no context: never saw A's write
    assert concurrent(socks.clock, shoes.clock)
    assert [v.value for v in store.get("cart:7")] == ["add socks", "add shoes"]
    merged = store.resolve("cart:7", "add socks and shoes", "A")
    assert merged.clock.as_dict() == {"A": 2, "B": 1}
    assert [v.value for v in store.get("cart:7")] == ["add socks and shoes"]
    informed = store.put("cart:7", "add hat", "B", context=merged.clock)
    assert [v.value for v in store.get("cart:7")] == ["add hat"]  # dominates, so it replaces
    assert informed.clock.as_dict() == {"A": 2, "B": 2}
    with pytest.raises(ValidationError):
        store.put("", "x", "A")


def test_last_writer_wins_drops_the_later_write_when_one_clock_runs_fast() -> None:
    lww = LastWriterWinsStore()
    assert lww.put("cart:7", "add socks", 500_124.0) is True  # A's clock is 1 ms fast
    assert lww.put("cart:7", "add shoes", 500_123.0) is False  # B wrote later in real time
    assert lww.get("cart:7") == "add socks"
    assert lww.discarded == 1
    assert lww.put("cart:7", "add hat", 500_124.0) is False  # a tie also loses
    assert lww.discarded == 2
    assert lww.get("missing") is None


def test_hybrid_logical_clock_keeps_causality_while_tracking_physical_time() -> None:
    clock_a, clock_b = FakeClock(start=500.000), FakeClock(start=499.998)
    hlc_a, hlc_b = HybridLogicalClock(clock_a), HybridLogicalClock(clock_b)
    first, second = hlc_a.now(), hlc_a.now()
    assert (first, second) == (HybridTimestamp(500_000, 0), HybridTimestamp(500_000, 1))
    received = hlc_b.update(second)  # B is 2 ms behind but must not order before A
    assert received == HybridTimestamp(500_000, 2)
    clock_b.advance(0.001)
    assert hlc_b.now() == HybridTimestamp(500_000, 3)  # still behind: only the counter moves
    clock_b.advance(0.010)
    assert hlc_b.now() == HybridTimestamp(500_009, 0)  # physical caught up, counter resets
    assert first < second < received


def test_hybrid_logical_clock_rejects_a_remote_clock_beyond_the_drift_bound() -> None:
    hlc = HybridLogicalClock(FakeClock(start=500.000), max_drift_ms=250)
    assert hlc.update(HybridTimestamp(500_200, 7)) == HybridTimestamp(500_200, 8)
    with pytest.raises(ValidationError):
        hlc.update(HybridTimestamp(500_400, 0))  # 400 ms ahead: a broken clock, not an event
    with pytest.raises(ValidationError):
        HybridLogicalClock(FakeClock(), max_drift_ms=0)


def test_concurrent_ticks_produce_unique_ordered_values() -> None:
    lamport = LamportClock("A")
    hlc = HybridLogicalClock(FakeClock(start=500.000))
    with ThreadPoolExecutor(max_workers=8) as pool:
        lamport_times = list(pool.map(lambda _: lamport.tick(), range(800)))
        hlc_times = list(pool.map(lambda _: hlc.now(), range(400)))
    assert sorted(lamport_times) == list(range(1, 801))  # no lost update under the lock
    assert lamport.time == 800
    assert len(set(hlc_times)) == 400
    assert sorted(hlc_times)[-1] == HybridTimestamp(500_000, 399)  # one frozen millisecond
