"""A command front end: the Facade that turns ``SET a 1`` into a call on the store."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from common import HandbookError
from lld.kv_store_transactions.models import CommandError, Value
from lld.kv_store_transactions.services import KVStore

NIL = "(nil)"
OK = "OK"
EXPIRY_FLAG = "EX"


# --8<-- [start:parser]
class CommandParser:
    """One line in, one line out - the shape an interviewer asks you to drive the store with.

    A Facade: it owns argument counts, type coercion and the reply format, so the
    store keeps a Python API and never grows string handling. The dispatch table
    means adding a verb is one method plus one entry.
    """

    def __init__(self, store: KVStore) -> None:
        self._store = store
        self._verbs: dict[str, Callable[[Sequence[str]], str]] = {
            "GET": self._get,
            "SET": self._set,
            "DEL": self._delete,
            "DELETE": self._delete,
            "EXISTS": self._exists,
            "INCR": self._incr,
            "DECR": self._decr,
            "SCAN": self._scan,
            "COUNT": self._count,
            "BEGIN": self._begin,
            "COMMIT": self._commit,
            "ROLLBACK": self._rollback,
        }

    def execute(self, line: str) -> str:
        parts = line.split()
        if not parts:
            raise CommandError("empty command")
        verb, args = parts[0].upper(), parts[1:]
        handler = self._verbs.get(verb)
        if handler is None:
            raise CommandError(f"unknown command {parts[0]!r}")
        return handler(args)

    def run(self, script: Iterable[str]) -> list[str]:
        """Run a script, turning a domain error into its message instead of raising."""
        replies: list[str] = []
        for line in script:
            try:
                replies.append(self.execute(line))
            except HandbookError as exc:
                replies.append(f"ERR {exc}")
        return replies

    # -- verbs -------------------------------------------------------------------
    def _get(self, args: Sequence[str]) -> str:
        value = self._store.get(self._one(args, "GET"))
        return NIL if value is None else str(value)

    def _set(self, args: Sequence[str]) -> str:
        if len(args) not in (2, 4):
            raise CommandError("usage: SET <key> <value> [EX <seconds>]")
        ttl: float | None = None
        if len(args) == 4:
            if args[2].upper() != EXPIRY_FLAG:
                raise CommandError(f"usage: SET <key> <value> [{EXPIRY_FLAG} <seconds>]")
            ttl = float(args[3])
        self._store.set(args[0], _coerce(args[1]), ttl=ttl)
        return OK

    def _delete(self, args: Sequence[str]) -> str:
        return "1" if self._store.delete(self._one(args, "DEL")) else "0"

    def _exists(self, args: Sequence[str]) -> str:
        return "1" if self._store.exists(self._one(args, "EXISTS")) else "0"

    def _incr(self, args: Sequence[str]) -> str:
        return str(self._store.incr(args[0], int(args[1]) if len(args) > 1 else 1))

    def _decr(self, args: Sequence[str]) -> str:
        return str(self._store.decr(args[0], int(args[1]) if len(args) > 1 else 1))

    def _scan(self, args: Sequence[str]) -> str:
        pairs = self._store.scan(args[0] if args else "")
        return " ".join(f"{key}={value}" for key, value in pairs) or "(empty)"

    def _count(self, args: Sequence[str]) -> str:
        return str(self._store.count(_coerce(self._one(args, "COUNT"))))

    def _begin(self, args: Sequence[str]) -> str:
        self._store.begin()
        return f"{OK} depth={self._store.depth}"

    def _commit(self, args: Sequence[str]) -> str:
        self._store.commit()
        return f"{OK} depth={self._store.depth}"

    def _rollback(self, args: Sequence[str]) -> str:
        self._store.rollback()
        return f"{OK} depth={self._store.depth}"

    @staticmethod
    def _one(args: Sequence[str], verb: str) -> str:
        if len(args) != 1:
            raise CommandError(f"usage: {verb} <key>")
        return args[0]


def _coerce(text: str) -> Value:
    """Integers stay integers so INCR works; everything else is a string."""
    try:
        return int(text)
    except ValueError:
        return text


# --8<-- [end:parser]
