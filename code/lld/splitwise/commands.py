"""Command objects for the operations a user is allowed to undo.

Only mutations that a person can reasonably take back are commands: adding and
deleting an expense. Settling up is deliberately *not* undoable -- money moved
outside the app -- which is the kind of boundary an interviewer wants you to
draw rather than pattern-matching Command onto everything.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Protocol

from common import InvalidStateError, Money
from lld.splitwise.models import Expense, SplitType
from lld.splitwise.services import ExpenseService


# --8<-- [start:commands]
class ExpenseCommand(Protocol):
    """A request as an object: it can run, and it knows how to take itself back."""

    def execute(self) -> Expense: ...

    def undo(self) -> Expense: ...


class AddExpenseCommand:
    """Adds an expense on ``execute``; deletes exactly that expense on ``undo``."""

    def __init__(
        self,
        service: ExpenseService,
        group_id: str,
        description: str,
        paid_by: Mapping[str, Money],
        participant_ids: Sequence[str],
        split_type: SplitType | str = SplitType.EQUAL,
        weights: Sequence[int] | None = None,
        actor_id: str | None = None,
    ) -> None:
        self._service = service
        self._group_id = group_id
        self._args = (description, dict(paid_by), list(participant_ids), split_type, weights, actor_id)
        self._actor_id = actor_id or "system"
        self._expense_id: str | None = None

    def execute(self) -> Expense:
        description, paid_by, participants, split_type, weights, actor_id = self._args
        expense = self._service.add_expense(
            self._group_id, description, paid_by, participants, split_type, weights, actor_id
        )
        self._expense_id = expense.id
        return expense

    def undo(self) -> Expense:
        if self._expense_id is None:
            raise InvalidStateError("cannot undo a command that never ran")
        return self._service.delete_expense(self._group_id, self._expense_id, self._actor_id)


class DeleteExpenseCommand:
    """Deletes an expense on ``execute``; restores the same version on ``undo``."""

    def __init__(self, service: ExpenseService, group_id: str, expense_id: str, actor_id: str) -> None:
        self._service = service
        self._group_id = group_id
        self._expense_id = expense_id
        self._actor_id = actor_id

    def execute(self) -> Expense:
        return self._service.delete_expense(self._group_id, self._expense_id, self._actor_id)

    def undo(self) -> Expense:
        return self._service.restore_expense(self._group_id, self._expense_id, self._actor_id)


class CommandHistory:
    """A per-user undo stack. The lock keeps two devices from popping the same command."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done: list[ExpenseCommand] = []

    def run(self, command: ExpenseCommand) -> Expense:
        result = command.execute()
        with self._lock:
            self._done.append(command)
        return result

    def undo_last(self) -> Expense:
        with self._lock:
            if not self._done:
                raise InvalidStateError("nothing to undo")
            command = self._done.pop()
        return command.undo()

    def depth(self) -> int:
        with self._lock:
            return len(self._done)


# --8<-- [end:commands]
