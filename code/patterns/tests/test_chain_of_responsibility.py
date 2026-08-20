"""Chain of Responsibility: each slot takes its share, the dispenser commits only complete plans."""

from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, Money, ValidationError
from patterns.chain_of_responsibility import (
    CannotDispenseError,
    CashDispenser,
    CashRequest,
    DenominationHandler,
    FraudRule,
    LogRecord,
    Payment,
    at_least,
    denied_country,
    first_rejection,
    over_limit,
    redact,
    run_stages,
    too_many_attempts,
)


def make_dispenser() -> CashDispenser:
    # deliberately out of order: the dispenser sorts the slots, largest note first
    return CashDispenser(
        [DenominationHandler(100, 10), DenominationHandler(2000, 2), DenominationHandler(500, 5)]
    )


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (100, {100: 1}),
        (600, {500: 1, 100: 1}),
        (3700, {2000: 1, 500: 3, 100: 2}),
        (7500, {2000: 2, 500: 5, 100: 10}),  # empties every slot
    ],
)
def test_each_slot_takes_its_share_largest_note_first(amount: int, expected: dict[int, int]) -> None:
    dispenser = make_dispenser()
    assert dispenser.withdraw(amount) == expected
    assert dispenser.inventory == {2000: 2 - expected.get(2000, 0), 500: 5 - expected.get(500, 0), 100: 10 - expected.get(100, 0)}


def test_slots_are_chained_by_denomination_whatever_order_they_arrive_in() -> None:
    assert list(make_dispenser().inventory) == [2000, 500, 100]
    with pytest.raises(ValidationError):
        CashDispenser([])
    with pytest.raises(ValidationError):
        CashDispenser([DenominationHandler(100, 1), DenominationHandler(100, 2)])


def test_a_failed_plan_leaves_every_slot_untouched() -> None:
    dispenser = make_dispenser()
    before = dispenser.inventory
    with pytest.raises(CannotDispenseError, match="short by 500") as info:
        dispenser.withdraw(8000)  # more than the slots hold in total
    assert isinstance(info.value, ConflictError)
    assert dispenser.inventory == before

    # the large note fits but the small slot cannot finish: still nothing is dispensed
    short = CashDispenser([DenominationHandler(2000, 1), DenominationHandler(100, 3)])
    with pytest.raises(CannotDispenseError, match="short by 100"):
        short.withdraw(2400)
    assert short.inventory == {2000: 1, 100: 3}


@pytest.mark.parametrize("amount", [0, -100, 250])
def test_validation_happens_before_the_chain_runs(amount: int) -> None:
    dispenser = make_dispenser()
    with pytest.raises(ValidationError):
        dispenser.withdraw(amount)
    assert dispenser.inventory == {2000: 2, 500: 5, 100: 10}


def test_a_handler_plans_without_touching_its_own_count() -> None:
    slot = DenominationHandler(500, 2)
    plan = slot.handle(CashRequest(amount=1200, remaining=1200))
    assert plan.notes == {500: 2}
    assert plan.remaining == 200  # no next link: the remainder comes back unhandled
    assert slot.count == 2
    slot.dispense(2)
    assert slot.count == 0
    with pytest.raises(CannotDispenseError):
        slot.dispense(1)


def test_pure_chain_stops_at_the_first_rule_with_an_opinion() -> None:
    asked: list[str] = []

    def spy(name: str, verdict: str | None) -> FraudRule:
        def rule(_payment: Payment) -> str | None:
            asked.append(name)
            return verdict

        return rule

    payment = Payment(Money.of("10.00"), "US", 0)
    rules = [spy("a", None), spy("b", "b says no"), spy("c", "never consulted")]
    assert first_rejection(rules, payment) == "b says no"
    assert asked == ["a", "b"]
    assert first_rejection([spy("only", None)], payment) is None


def test_fraud_rules_read_as_a_list_and_reorder_as_a_list() -> None:
    country = denied_country(frozenset({"KP"}))
    limit = over_limit(Money.of("1000.00"))
    velocity = too_many_attempts(3)
    risky = Payment(Money.of("5000.00"), "KP", 9)
    assert first_rejection([country, limit, velocity], risky) == "country KP is denied"
    assert first_rejection([velocity, limit, country], risky) == "9 attempts in the last hour"
    assert first_rejection([country, limit, velocity], Payment(Money.of("5.00"), "US", 0)) is None


def test_generator_stages_hand_survivors_down_the_line_lazily() -> None:
    seen: list[str] = []

    def tap(records: Iterable[LogRecord]) -> Iterator[LogRecord]:
        for record in records:
            seen.append(record.message)
            yield record

    records = [
        LogRecord(10, "app.db", "connect token=abc"),
        LogRecord(40, "app.db", "timeout token=abc"),
    ]
    stream = run_stages([at_least(20), redact("abc"), tap], records)
    assert seen == []  # nothing ran yet
    assert [record.message for record in stream] == ["timeout token=***"]
    assert seen == ["timeout token=***"]  # the dropped record never reached the later stage


def test_concurrent_withdrawals_never_overdraw_a_slot() -> None:
    dispenser = CashDispenser([DenominationHandler(100, 50)])

    def attempt(_: int) -> int:
        try:
            dispenser.withdraw(100)
        except CannotDispenseError:
            return 0
        return 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        served = sum(pool.map(attempt, range(80)))
    assert served == 50
    assert dispenser.inventory == {100: 0}
