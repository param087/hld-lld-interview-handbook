"""Transactions as Command objects with one Template Method: validate, perform, receipt.

Every transaction the machine offers is a class here. The state machine in ``states.py``
decides *whether* a transaction may start; these classes decide what it does.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from common import Money, ValidationError
from lld.atm.models import (
    AtmStateName,
    DenominationError,
    Receipt,
    TransactionRecord,
    TransactionType,
)

if TYPE_CHECKING:  # pragma: no cover - the ATM is only needed for type hints
    from lld.atm.services import ATM


# --8<-- [start:template]
class AtmTransaction(ABC):
    """Command + Template Method.

    ``execute`` is fixed: validate, perform, print. Subclasses own ``perform``; only
    the withdrawal needs to touch the hardware, and only it overrides ``validate``.
    """

    transaction_type: ClassVar[TransactionType]

    def __init__(self, atm: ATM, account_id: str, amount: Money | None = None) -> None:
        self.atm = atm
        self.account_id = account_id
        self.amount = amount

    def execute(self) -> Receipt:
        self.validate()
        record, notes = self.perform()
        receipt = Receipt(atm_id=self.atm.id, record=record, notes=notes)
        self.atm.printer.print_receipt(receipt)
        return receipt

    def validate(self) -> None:
        if self.amount is not None and self.amount.cents <= 0:
            raise ValidationError(f"{self.transaction_type} amount must be positive")

    @abstractmethod
    def perform(self) -> tuple[TransactionRecord, dict[Money, int]]:
        """Do the work and return the ledger record plus any notes handed over."""


class WithdrawalTransaction(AtmTransaction):
    """The atomicity crux: reserve, dispense, commit - and roll back if the notes jam."""

    transaction_type = TransactionType.WITHDRAWAL

    def validate(self) -> None:
        super().validate()
        if self.amount is None:
            raise ValidationError("a withdrawal needs an amount")
        smallest = self.atm.dispenser.smallest_note()
        if self.amount.cents % smallest.cents:
            raise DenominationError(f"{self.amount} is not a multiple of {smallest}")
        self.atm.dispenser.plan(self.amount)  # fails now, before any money is promised

    def perform(self) -> tuple[TransactionRecord, dict[Money, int]]:
        bank, dispenser = self.atm.bank, self.atm.dispenser
        amount = self.amount if self.amount is not None else Money(0)
        reservation = bank.reserve(self.account_id, amount)  # 1. promise the money
        self.atm.enter(AtmStateName.DISPENSING)
        try:
            notes = dispenser.dispense(amount)  # 2. hand it over
        except Exception:
            bank.release(reservation)  # nothing left the machine, so un-promise it
            raise
        return bank.commit(reservation), notes  # 3. only now is the balance really down


# --8<-- [end:template]


# --8<-- [start:commands]
class DepositTransaction(AtmTransaction):
    transaction_type = TransactionType.DEPOSIT

    def perform(self) -> tuple[TransactionRecord, dict[Money, int]]:
        amount = self.amount if self.amount is not None else Money(0)
        return self.atm.bank.deposit(self.account_id, amount), {}


class TransferTransaction(AtmTransaction):
    transaction_type = TransactionType.TRANSFER

    def __init__(self, atm: ATM, account_id: str, amount: Money | None, target_id: str) -> None:
        super().__init__(atm, account_id, amount)
        self.target_id = target_id

    def perform(self) -> tuple[TransactionRecord, dict[Money, int]]:
        amount = self.amount if self.amount is not None else Money(0)
        return self.atm.bank.transfer(self.account_id, self.target_id, amount), {}


class BalanceInquiry(AtmTransaction):
    transaction_type = TransactionType.BALANCE_INQUIRY

    def perform(self) -> tuple[TransactionRecord, dict[Money, int]]:
        return self.atm.bank.record_inquiry(self.account_id), {}


_TRANSACTIONS: dict[TransactionType, type[AtmTransaction]] = {
    TransactionType.WITHDRAWAL: WithdrawalTransaction,
    TransactionType.DEPOSIT: DepositTransaction,
    TransactionType.TRANSFER: TransferTransaction,
    TransactionType.BALANCE_INQUIRY: BalanceInquiry,
}


class TransactionFactory:
    """Adding "pay a bill" means one class and one line here - nothing else changes."""

    @staticmethod
    def create(
        transaction_type: TransactionType, atm: ATM, account_id: str, amount: Money | None = None, **extra: str
    ) -> AtmTransaction:
        klass = _TRANSACTIONS[transaction_type]
        return klass(atm, account_id, amount, **extra)


# --8<-- [end:commands]
