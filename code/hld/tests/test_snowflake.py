from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, FakeClock, InvalidStateError, ValidationError
from hld.snowflake import (
    DEFAULT_EPOCH_MS,
    ClockDriftError,
    Layout,
    MachineIdRegistry,
    SnowflakeGenerator,
)

START = 1_750_000_000.0  # seconds; comfortably after the 2024 epoch


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


def test_layout_defaults_and_validation() -> None:
    layout = Layout()
    assert (layout.max_machine_id, layout.max_sequence) == (1023, 4095)
    assert layout.lifetime_ms == 2**41
    assert layout.timestamp_shift == 22 and layout.machine_shift == 12
    with pytest.raises(ValidationError):
        Layout(timestamp_bits=40)  # 40 + 10 + 12 != 63
    with pytest.raises(ValidationError):
        Layout(timestamp_bits=63, machine_bits=0, sequence_bits=0)


@pytest.mark.parametrize(("machine_id", "sequence"), [(0, 0), (1023, 4095), (7, 42)])
def test_compose_decompose_roundtrip(machine_id: int, sequence: int) -> None:
    layout = Layout()
    elapsed_ms = 123_456_789
    parts = layout.decompose(layout.compose(elapsed_ms, machine_id, sequence))
    assert parts.timestamp_ms == elapsed_ms + DEFAULT_EPOCH_MS
    assert (parts.machine_id, parts.sequence) == (machine_id, sequence)
    with pytest.raises(ValidationError):
        layout.decompose(-1)


def test_ids_are_unique_increasing_and_borrow_a_millisecond_on_overflow(clock: FakeClock) -> None:
    gen = SnowflakeGenerator(machine_id=5, clock=clock)
    ids = [gen.next_id() for _ in range(5000)]  # 4096 fit in one ms, the rest borrow the next
    assert len(set(ids)) == 5000
    assert ids == sorted(ids)
    first, last = gen.decompose(ids[0]), gen.decompose(ids[-1])
    assert gen.decompose(ids[4095]).sequence == 4095
    assert gen.decompose(ids[4096]).sequence == 0
    assert last.timestamp_ms - first.timestamp_ms == 1
    assert last.sequence == 5000 - 4096 - 1
    assert gen.borrowed_ms == 1
    assert all(gen.decompose(i).machine_id == 5 for i in ids[:10])


def test_ids_are_k_sortable_across_machines(clock: FakeClock) -> None:
    high_machine = SnowflakeGenerator(machine_id=1023, clock=clock)
    low_machine = SnowflakeGenerator(machine_id=0, clock=clock)
    earlier = high_machine.next_id()
    clock.advance(0.001)
    later = low_machine.next_id()
    assert later > earlier  # time bits dominate machine bits
    assert low_machine.decompose(later).timestamp_ms == high_machine.decompose(earlier).timestamp_ms + 1


def test_backwards_clock_within_tolerance_keeps_ids_increasing(clock: FakeClock) -> None:
    gen = SnowflakeGenerator(machine_id=1, clock=clock, max_drift_ms=5)
    before = gen.next_id()
    clock.set(START - 0.004)  # NTP step of 4 ms
    after = gen.next_id()
    assert after > before
    assert gen.decompose(after).timestamp_ms == gen.decompose(before).timestamp_ms  # logical ms held
    clock.set(START + 0.010)  # wall clock catches up and overtakes
    assert gen.decompose(gen.next_id()).timestamp_ms == gen.decompose(before).timestamp_ms + 10


def test_backwards_clock_beyond_tolerance_raises_and_leaves_state_intact(clock: FakeClock) -> None:
    gen = SnowflakeGenerator(machine_id=1, clock=clock, max_drift_ms=5)
    before = gen.next_id()
    clock.set(START - 0.050)
    with pytest.raises(ClockDriftError):
        gen.next_id()
    clock.set(START + 0.001)
    after = gen.next_id()
    assert after > before
    assert gen.decompose(after).sequence == 0  # the failed call did not consume a sequence slot


def test_generator_argument_validation(clock: FakeClock) -> None:
    with pytest.raises(ValidationError):
        SnowflakeGenerator(machine_id=1024, clock=clock)
    with pytest.raises(ValidationError):
        SnowflakeGenerator(machine_id=0, clock=clock, max_drift_ms=-1)
    with pytest.raises(InvalidStateError):
        SnowflakeGenerator(machine_id=0, clock=FakeClock(start=0.0)).next_id()  # before the epoch


def test_registry_assigns_lowest_free_id_and_reclaims_expired_leases(clock: FakeClock) -> None:
    registry = MachineIdRegistry(capacity=3, lease_seconds=30, clock=clock)
    assert registry.register("a") == 0
    assert registry.register("b") == 1
    assert registry.register("a") == 0  # fast restart inside the lease: same id back
    clock.advance(20)
    registry.renew("a", 0)
    clock.advance(11)  # b's lease (30 s) has expired, a's (renewed at 20 s) has not
    assert registry.holder(1) is None
    assert registry.holder(0) == "a"
    assert registry.register("c") == 1
    with pytest.raises(InvalidStateError):
        registry.renew("b", 1)
    assert registry.register("d") == 2
    with pytest.raises(ConflictError):
        registry.register("e")
    registry.release("d", 2)
    assert registry.register("e") == 2


def test_concurrent_minting_never_duplicates(clock: FakeClock) -> None:
    gen = SnowflakeGenerator(machine_id=9, clock=clock, max_drift_ms=100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: gen.next_id(), range(16_000)))
    assert len(set(ids)) == 16_000
    assert gen.borrowed_ms == 16_000 // 4096  # frozen clock: every extra ms was borrowed
