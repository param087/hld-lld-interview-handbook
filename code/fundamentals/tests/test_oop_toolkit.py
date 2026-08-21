"""Object-oriented Python: the guarantees each construct actually buys you."""

import pytest

from common import ConflictError, InvalidStateError, Money, NotFoundError, ValidationError
from fundamentals.oop_toolkit import (
    Amenity,
    AuditedSeatMap,
    Booking,
    BookingStatus,
    ClassPricing,
    EmailNotifier,
    FlatPricing,
    Notifier,
    PricingRule,
    Registry,
    Seat,
    SeatClass,
    SeatHold,
    SeatMap,
    WaitlistEntry,
    hold_labels,
    identity_vs_equality,
    row_pickers,
    seats_matching,
)


def seat_map() -> SeatMap:
    return SeatMap(
        Seat(row, column, seat_class)
        for row, seat_class in ((1, SeatClass.BUSINESS), (2, SeatClass.PREMIUM))
        for column in "ABCD"
    )


def test_frozen_value_object_normalises_hashes_and_orders() -> None:
    parsed = Seat.parse("12a")
    assert parsed == Seat(12, "A") and parsed.label == "12A"
    assert len({Seat(1, "A"), Seat.parse("1A"), Seat(1, "B")}) == 2  # hashable by value
    assert sorted([Seat(2, "A"), Seat(1, "B")]) == [Seat(1, "B"), Seat(2, "A")]
    assert Seat(1, "A", amenities=Amenity.WINDOW) == Seat(1, "A")  # compare=False field
    with pytest.raises(AttributeError):
        parsed.row = 3  # type: ignore[misc]


def test_replace_copies_and_never_mutates_the_original() -> None:
    economy = Seat(3, "C")
    business = economy.upgraded(SeatClass.BUSINESS)
    assert business.seat_class is SeatClass.BUSINESS
    assert economy.seat_class is SeatClass.ECONOMY and economy is not business


@pytest.mark.parametrize("label", ["0A", "1", "1AB", "A1", "row twelve", "12!"])
def test_post_init_and_parse_reject_impossible_seats(label: str) -> None:
    with pytest.raises(ValidationError):
        Seat.parse(label)


def test_entity_equality_is_identity_by_id_not_by_field_values() -> None:
    held = Booking("BK-1", "SHOW-7")
    confirmed = Booking("BK-1", "SHOW-7", [Seat(1, "A")], BookingStatus.CONFIRMED)
    assert held == confirmed and hash(held) == hash(confirmed)  # same booking, later state
    assert held != Booking("BK-2", "SHOW-7")
    assert held.__eq__("BK-1") is NotImplemented  # defer, do not answer False
    assert Booking("BK-3", "SHOW-7").seats is not Booking("BK-4", "SHOW-7").seats


def test_guarded_transition_rejects_a_second_confirm_and_an_empty_booking() -> None:
    booking = Booking("BK-1", "SHOW-7", [Seat(1, "A")])
    booking.confirm()
    assert booking.status is BookingStatus.CONFIRMED
    with pytest.raises(InvalidStateError):
        booking.confirm()
    with pytest.raises(ValidationError):
        Booking("BK-2", "SHOW-7").confirm()


def test_str_enum_members_are_strings_and_flags_combine() -> None:
    assert SeatClass.PREMIUM == "premium" and f"{SeatClass.PREMIUM}" == "premium"
    assert SeatClass("economy") is SeatClass.ECONOMY  # lookup by value, one object
    window_power = Amenity.WINDOW | Amenity.POWER
    assert Amenity.WINDOW in window_power and Amenity.AISLE not in window_power
    assert window_power & Amenity.POWER is Amenity.POWER


def test_protocol_is_structural_while_abc_is_nominal() -> None:
    class RecordingPricing:  # no base class, no import of PricingRule
        def price(self, seat: Seat) -> Money:
            return Money(0)

    for rule in (ClassPricing(), FlatPricing(), RecordingPricing()):
        assert isinstance(rule, PricingRule)
        assert PricingRule not in type(rule).__mro__
    assert not isinstance(object(), PricingRule)

    class BrokenNotifier(Notifier):
        pass

    with pytest.raises(TypeError):
        BrokenNotifier()  # type: ignore[abstract]
    assert issubclass(EmailNotifier, Notifier)


def test_container_dunders_replace_accessor_methods() -> None:
    seats = seat_map()
    assert len(seats) == 8
    assert "2c" in seats and Seat(1, "A", SeatClass.BUSINESS) in seats and "9Z" not in seats
    assert seats["1a"].seat_class is SeatClass.BUSINESS
    assert [seat.label for seat in seats][:3] == ["1A", "1B", "1C"]  # __iter__, sorted
    assert seats.labels() == sorted(seats.labels())  # method inherited from the mixin
    assert repr(seats) == "SeatMap(rows=2, seats=8)"
    with pytest.raises(NotFoundError):
        seats["9Z"]


def test_context_manager_releases_the_hold_even_when_the_block_raises() -> None:
    seats, held = seat_map(), set()
    with pytest.raises(RuntimeError), SeatHold(seats, ["1A", "1B"], held):
        assert held == {"1A", "1B"}
        raise RuntimeError("payment provider timed out")
    assert held == set()  # __exit__ ran, and it did not swallow the error

    with SeatHold(seats, ["1A"], held):
        with pytest.raises(ConflictError):
            with SeatHold(seats, ["1A", "2A"], held):
                pytest.fail("the second hold must not open")
        assert held == {"1A"}  # the failed __enter__ left nothing behind


def test_total_ordering_derives_the_comparisons_from_one_key() -> None:
    low = WaitlistEntry("BK-3", priority=1, sequence=2)
    high_late = WaitlistEntry("BK-4", priority=5, sequence=3)
    high_early = WaitlistEntry("BK-5", priority=5, sequence=1)
    assert sorted([low, high_late, high_early]) == [high_early, high_late, low]
    assert high_early < high_late <= low and low > high_early
    assert high_early != high_late and hash(high_early) != hash(low)


def test_generic_registry_chains_on_self_and_literal_picks_the_order() -> None:
    registry: Registry[Booking] = Registry(key=lambda booking: booking.booking_id)
    result = registry.add(Booking("BK-9", "S")).add(Booking("BK-1", "S"))
    assert result is registry  # add returns Self, so calls chain
    assert [b.booking_id for b in registry.list()] == ["BK-9", "BK-1"]
    assert [b.booking_id for b in registry.list(order="key")] == ["BK-1", "BK-9"]
    assert registry.get("BK-1").show_id == "S"
    with pytest.raises(NotFoundError):
        registry.get("BK-404")


def test_composition_wraps_without_inheriting_the_whole_surface() -> None:
    inner = seat_map()
    audited = AuditedSeatMap(inner)
    assert len(audited) == len(inner) and "1a" in audited
    audited["1a"], audited["2B"]
    assert audited.reads == ("1A", "2B")
    assert not isinstance(audited, SeatMap)  # it is a collaborator, not a subtype


def test_the_three_gotchas_behave_after_the_fix() -> None:
    assert hold_labels(["4a"]) == ["4A"] and hold_labels(["4b"]) == ["4B"]  # no shared default
    seats = seat_map()
    picked = [{seat.row for seat in seats_matching(seats, p)} for p in row_pickers([1, 2])]
    assert picked == [{1}, {2}]  # each closure kept its own row
    same_object, same_value, enum_is_singleton = identity_vs_equality("2B")
    assert (same_object, same_value, enum_is_singleton) == (False, True, True)
