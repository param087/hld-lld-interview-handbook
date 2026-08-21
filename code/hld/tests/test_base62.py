from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, FakeClock, ValidationError
from hld.base62 import (
    ALPHABET,
    CODE_LENGTH,
    CounterCodes,
    HashCodes,
    InMemoryCodeStore,
    SnowflakeCodes,
    code_space,
    decode,
    encode,
    scramble,
    unscramble,
)


@pytest.fixture
def store() -> InMemoryCodeStore:
    return InMemoryCodeStore()


@pytest.mark.parametrize(
    ("number", "code"),
    [(0, "0"), (1, "1"), (61, "z"), (62, "10"), (3843, "zz"), (238_328, "1000")],
)
def test_base62_matches_hand_computed_values(number: int, code: str) -> None:
    assert encode(number) == code
    assert decode(code) == number


def test_base62_round_trips_and_pads_to_a_fixed_width() -> None:
    for number in (0, 7, 61, 62, 12_345_678, code_space() - 1):
        padded = encode(number, CODE_LENGTH)
        assert len(padded) == CODE_LENGTH
        assert decode(padded) == number
    assert encode(code_space() - 1, CODE_LENGTH) == ALPHABET[-1] * CODE_LENGTH
    assert code_space() == 3_521_614_606_208  # 62^7, the runway the interviewer asks about


def test_base62_rejects_bad_input() -> None:
    with pytest.raises(ValidationError):
        encode(-1)
    with pytest.raises(ValidationError):
        decode("")
    with pytest.raises(ValidationError):
        decode("abc-def")  # '-' is not a base62 digit
    with pytest.raises(ValidationError):
        code_space(0)


def test_scramble_is_a_reversible_permutation_that_hides_the_counter() -> None:
    values = [scramble(counter) for counter in range(2_000)]
    assert len(set(values)) == 2_000  # bijective: no counter collides with another
    assert all(unscramble(value) == counter for counter, value in enumerate(values))
    # consecutive counters must not produce consecutive (guessable) codes
    assert all(abs(values[i + 1] - values[i]) > 1_000 for i in range(50))
    with pytest.raises(ValidationError):
        scramble(1 << 41)


def test_counter_codes_are_seven_chars_and_never_collide(store: InMemoryCodeStore) -> None:
    codes = [CounterCodes(store, start=1).shorten(f"https://a.test/{i}").code for i in range(1)]
    strategy = CounterCodes(store, start=100)
    codes += [strategy.shorten(f"https://b.test/{i}").code for i in range(500)]
    assert all(len(code) == CODE_LENGTH for code in codes)
    assert len(set(codes)) == len(codes)
    assert strategy.collisions == 0
    assert store.resolve(codes[-1]) == "https://b.test/499"


def test_snowflake_codes_sort_by_creation_time(store: InMemoryCodeStore) -> None:
    clock = FakeClock(start=1_750_000_000.0)
    strategy = SnowflakeCodes(store, machine_id=3, clock=clock)
    codes = []
    for i in range(5):
        clock.advance(0.5)
        codes.append(strategy.shorten(f"https://c.test/{i}").code)
    assert codes == sorted(codes)  # equal length, so lexicographic order is numeric order
    assert len({len(code) for code in codes}) == 1
    assert len(codes[0]) > CODE_LENGTH  # a timestamp costs characters


def test_hash_codes_deduplicate_and_retry_on_a_real_collision(store: InMemoryCodeStore) -> None:
    strategy = HashCodes(store)
    first = strategy.shorten("https://d.test/pricing")
    repeat = strategy.shorten("https://d.test/pricing")
    assert (first.code, first.attempts) == (repeat.code, 1)  # same URL, same row, no retry
    assert len(store) == 1

    crowded = HashCodes(InMemoryCodeStore(), length=1)  # only 62 codes exist
    links = [crowded.shorten(f"https://e.test/{i}") for i in range(30)]
    assert len({link.code for link in links}) == 30
    assert crowded.collisions > 0
    assert any(link.attempts > 1 for link in links)


def test_hash_codes_give_up_when_the_code_space_is_exhausted() -> None:
    strategy = HashCodes(InMemoryCodeStore(), length=1)
    with pytest.raises(ConflictError):
        for i in range(200):  # 62 codes and 8 attempts each: exhaustion is certain
            strategy.shorten(f"https://f.test/{i}")


def test_empty_url_is_rejected(store: InMemoryCodeStore) -> None:
    with pytest.raises(ValidationError):
        CounterCodes(store).shorten("   ")


def test_concurrent_shortening_never_hands_out_a_code_twice(store: InMemoryCodeStore) -> None:
    strategy = CounterCodes(store, start=1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda i: strategy.shorten(f"https://g.test/{i}").code, range(800)))
    assert len(set(codes)) == 800
    assert len(store) == 800
    assert strategy.collisions == 0
