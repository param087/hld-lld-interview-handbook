"""The State pattern: one class per machine state, and a base that refuses everything.

Writing the base as "no" and each state as a short list of "yes" is what makes this
worth six classes: an operation nobody allowed is impossible to reach by accident, and
the transition table is readable by looking at which methods each class overrides.
"""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING, ClassVar, NoReturn

from common import Money
from lld.atm.models import (
    AtmStateError,
    AtmStateName,
    Card,
    CardBlockedError,
    DispenserJamError,
    Receipt,
    TransactionRecord,
    TransactionType,
)
from lld.atm.transactions import TransactionFactory

if TYPE_CHECKING:  # pragma: no cover - type hints only
    from lld.atm.services import ATM


# --8<-- [start:base]
class ATMState(ABC):
    """Default behaviour: refuse. Subclasses override only what they permit."""

    name: ClassVar[AtmStateName]

    def insert_card(self, atm: ATM, card: Card) -> None:
        self._refuse("insert a card")

    def enter_pin(self, atm: ATM, pin: str) -> None:
        self._refuse("enter a PIN")

    def select_account(self, atm: ATM, account_id: str) -> None:
        self._refuse("select an account")

    def check_balance(self, atm: ATM) -> Money:
        self._refuse("check a balance")

    def withdraw(self, atm: ATM, amount: Money) -> Receipt:
        self._refuse("withdraw cash")

    def deposit(self, atm: ATM, amount: Money) -> Receipt:
        self._refuse("deposit cash")

    def transfer(self, atm: ATM, target_id: str, amount: Money) -> Receipt:
        self._refuse("transfer money")

    def mini_statement(self, atm: ATM) -> list[TransactionRecord]:
        self._refuse("print a statement")

    def cancel(self, atm: ATM) -> None:
        self._refuse("cancel")

    def _refuse(self, action: str) -> NoReturn:
        raise AtmStateError(f"cannot {action} while the machine is {self.name}")


class IdleState(ATMState):
    name = AtmStateName.IDLE

    def insert_card(self, atm: ATM, card: Card) -> None:
        if not card.is_usable():
            atm.reader.retain(card)
            raise CardBlockedError(f"card {card.number} is {card.status}; retained")
        atm.reader.accept(card)
        atm.screen.show(f"welcome {card.holder}, enter your PIN")
        atm.enter(AtmStateName.CARD_INSERTED)

    def cancel(self, atm: ATM) -> None:
        atm.screen.show("nothing to cancel")


class CardInsertedState(ATMState):
    name = AtmStateName.CARD_INSERTED

    def enter_pin(self, atm: ATM, pin: str) -> None:
        card = atm.require_card()
        try:
            accounts = atm.bank.authenticate(card.number, pin)
        except CardBlockedError:
            atm.reader.retain(card)
            atm.end_session()
            raise
        atm.begin_session(accounts)
        atm.screen.show(f"accounts: {', '.join(accounts)}")
        atm.enter(AtmStateName.AUTHENTICATED)

    def cancel(self, atm: ATM) -> None:
        atm.end_session()


# --8<-- [end:base]


# --8<-- [start:authenticated]
class AuthenticatedState(ATMState):
    """The only state that starts transactions. It owns the TRANSACTING/DISPENSING dance."""

    name = AtmStateName.AUTHENTICATED

    def select_account(self, atm: ATM, account_id: str) -> None:
        atm.set_account(account_id)

    def check_balance(self, atm: ATM) -> Money:
        return atm.bank.balance(atm.require_account())

    def mini_statement(self, atm: ATM) -> list[TransactionRecord]:
        return atm.bank.statement(atm.require_account(), limit=5)

    def withdraw(self, atm: ATM, amount: Money) -> Receipt:
        return self._run(atm, TransactionType.WITHDRAWAL, amount)

    def deposit(self, atm: ATM, amount: Money) -> Receipt:
        return self._run(atm, TransactionType.DEPOSIT, amount)

    def transfer(self, atm: ATM, target_id: str, amount: Money) -> Receipt:
        return self._run(atm, TransactionType.TRANSFER, amount, target_id=target_id)

    def cancel(self, atm: ATM) -> None:
        atm.end_session()

    def _run(
        self, atm: ATM, transaction_type: TransactionType, amount: Money, **extra: str
    ) -> Receipt:
        transaction = TransactionFactory.create(
            transaction_type, atm, atm.require_account(), amount, **extra
        )
        atm.enter(AtmStateName.TRANSACTING)
        try:
            receipt = transaction.execute()
        except DispenserJamError as exc:
            # The money is already un-reserved; the machine, however, is broken.
            atm.go_out_of_service(str(exc))
            raise
        except Exception:
            atm.enter(AtmStateName.AUTHENTICATED)  # a refusal keeps the session open
            raise
        atm.enter(AtmStateName.AUTHENTICATED)
        return receipt


class TransactingState(ATMState):
    """Transient. A second request arriving now is refused rather than queued."""

    name = AtmStateName.TRANSACTING


class DispensingState(ATMState):
    """Transient, and deliberately without ``cancel``: notes are already moving."""

    name = AtmStateName.DISPENSING


class OutOfServiceState(ATMState):
    name = AtmStateName.OUT_OF_SERVICE

    def insert_card(self, atm: ATM, card: Card) -> None:
        raise AtmStateError(f"{atm.id} is out of service: {atm.fault or 'unknown fault'}")


_STATES: dict[AtmStateName, ATMState] = {
    AtmStateName.IDLE: IdleState(),
    AtmStateName.CARD_INSERTED: CardInsertedState(),
    AtmStateName.AUTHENTICATED: AuthenticatedState(),
    AtmStateName.TRANSACTING: TransactingState(),
    AtmStateName.DISPENSING: DispensingState(),
    AtmStateName.OUT_OF_SERVICE: OutOfServiceState(),
}


def state_for(name: AtmStateName) -> ATMState:
    """States hold no data, so one instance each is shared by every machine (Flyweight)."""
    return _STATES[name]


# --8<-- [end:authenticated]
