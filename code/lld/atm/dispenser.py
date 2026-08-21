"""The cash side: a Chain of Responsibility over denominations and the hardware in front of it."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Protocol

from common import Money, ValidationError
from lld.atm.models import DenominationError, DispenserJamError, OutOfCashError

DEFAULT_NOTES: tuple[Money, ...] = (
    Money.of("100.00"),
    Money.of("50.00"),
    Money.of("20.00"),
    Money.of("10.00"),
)


# --8<-- [start:chain]
class DenominationHandler:
    """One link per note value: take what you can, hand the remainder to the next link.

    The textbook version is greedy and *wrong*: paying 60 from one 50 and three 20s
    fails if the 50 is taken first. Trying counts downwards and backtracking when the
    rest of the chain cannot finish costs four lines and makes the answer correct.
    """

    def __init__(self, note: Money, successor: DenominationHandler | None = None) -> None:
        if note.cents <= 0:
            raise ValidationError("a note must be worth something")
        self.note = note
        self.successor = successor

    def plan(self, amount: Money, available: Mapping[Money, int]) -> dict[Money, int]:
        """Notes to hand out, or ``DenominationError`` if this chain cannot make the amount."""
        if amount.is_zero():
            return {}
        most = min(amount.cents // self.note.cents, available.get(self.note, 0))
        for count in range(most, -1, -1):
            remainder = amount - self.note * count
            if remainder.is_zero():
                return {self.note: count} if count else {}
            if self.successor is None:
                continue
            try:
                rest = self.successor.plan(remainder, available)
            except DenominationError:
                continue  # back up and try one note fewer
            return {self.note: count, **rest} if count else rest
        raise DenominationError(f"cannot make {amount} from the notes in the machine")


def build_chain(notes: tuple[Money, ...] = DEFAULT_NOTES) -> DenominationHandler:
    """Factory: build from the smallest note up, so the head of the chain is the largest."""
    ordered = sorted(notes)
    if not ordered:
        raise ValidationError("a dispenser needs at least one denomination")
    chain = DenominationHandler(ordered[0])
    for note in ordered[1:]:
        chain = DenominationHandler(note, chain)
    return chain


# --8<-- [end:chain]


# --8<-- [start:dispenser]
class NoteFeeder(Protocol):
    """The hardware. Tests substitute one that jams."""

    def push(self, plan: Mapping[Money, int]) -> None: ...


class ReliableFeeder:
    def push(self, plan: Mapping[Money, int]) -> None:
        return None


class CashDispenser:
    """Owns the cassettes and the lock that protects them.

    ``_lock`` guards the note counts: two machines never share a dispenser, but the
    replenishing admin and a customer do, and a half-counted cassette is real money.
    """

    def __init__(
        self,
        inventory: Mapping[Money, int],
        feeder: NoteFeeder | None = None,
        chain: DenominationHandler | None = None,
    ) -> None:
        self._inventory: dict[Money, int] = dict(inventory)
        self._feeder = feeder or ReliableFeeder()
        self._chain = chain or build_chain(tuple(inventory))
        self._lock = threading.Lock()

    def total(self) -> Money:
        with self._lock:
            return self._total_unlocked()

    def smallest_note(self) -> Money:
        with self._lock:
            return min(self._inventory)

    def counts(self) -> dict[Money, int]:
        with self._lock:
            return dict(self._inventory)

    def plan(self, amount: Money) -> dict[Money, int]:
        """What would come out, without moving a note. Raises before anything is reserved."""
        with self._lock:
            if amount > self._total_unlocked():
                raise OutOfCashError(f"{self.__class__.__name__} holds {self._total_unlocked()}, less than {amount}")
            return self._chain.plan(amount, self._inventory)

    def dispense(self, amount: Money) -> dict[Money, int]:
        """Plan, push the notes, then decrement. A jam leaves the cassettes untouched."""
        with self._lock:
            if amount > self._total_unlocked():
                raise OutOfCashError(f"cassettes hold {self._total_unlocked()}, less than {amount}")
            plan = self._chain.plan(amount, self._inventory)
            self._feeder.push(plan)  # may raise DispenserJamError
            for note, count in plan.items():
                self._inventory[note] -= count
            return plan

    def replenish(self, notes: Mapping[Money, int]) -> Money:
        with self._lock:
            for note, count in notes.items():
                if count < 0:
                    raise ValidationError("cannot replenish a negative number of notes")
                self._inventory[note] = self._inventory.get(note, 0) + count
            return self._total_unlocked()

    def _total_unlocked(self) -> Money:
        total = Money(0)
        for note, count in self._inventory.items():
            total = total + note * count
        return total


class JammingFeeder:
    """Test/demo double: the pick roller fails before any note leaves the machine."""

    def push(self, plan: Mapping[Money, int]) -> None:
        raise DispenserJamError(f"note feeder jammed while picking {len(plan)} denomination(s)")


# --8<-- [end:dispenser]
