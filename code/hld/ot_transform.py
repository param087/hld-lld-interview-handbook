"""Operational transformation for a collaborative editor: insert/delete transform + sequencing.

The crux of the Google Docs design in one module:

* ``transform(op, against)`` rewrites an operation so it can be applied to a document that already
  has a concurrent operation applied. The four cases (insert/insert, insert/delete, delete/insert,
  delete/delete) are the whole algorithm, and the tie-break argument is the part candidates get
  wrong.
* ``DocumentServer`` is the central sequencer: one revision counter and one op log per document.
  A client submits an op plus the revision it was written against; the server rebases it over
  everything accepted since, applies it, and appends it. One document, one server, one order.
* ``ClientDocument`` is the other half: apply locally at once, keep the op in flight, and rebase
  incoming ops against it so both sides compute the same answer.

Convergence property (TP1): for concurrent ops ``a`` and ``b`` on document ``d``,
``apply(apply(d, a), transform(b, a))`` equals ``apply(apply(d, b), transform(a, b, op_first=True))``.
The test module asserts it over a matrix of op pairs and over randomised ops.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from common import InvalidStateError, ValidationError


# --8<-- [start:ops]
@dataclass(frozen=True, slots=True)
class Insert:
    """Insert ``text`` at character offset ``pos`` of the current document."""

    pos: int
    text: str

    def __post_init__(self) -> None:
        if self.pos < 0 or not self.text:
            raise ValidationError(f"invalid insert at {self.pos} of {self.text!r}")

    @property
    def length(self) -> int:
        return len(self.text)

    @property
    def end(self) -> int:
        return self.pos + self.length


@dataclass(frozen=True, slots=True)
class Delete:
    """Delete ``length`` characters starting at offset ``pos``."""

    pos: int
    length: int

    def __post_init__(self) -> None:
        if self.pos < 0 or self.length <= 0:
            raise ValidationError(f"invalid delete of {self.length} at {self.pos}")

    @property
    def end(self) -> int:
        return self.pos + self.length


Op = Insert | Delete


def apply_op(text: str, op: Op) -> str:
    """Apply one op. Offsets always refer to the document as it is *now*, never to a base version."""
    if isinstance(op, Insert):
        if op.pos > len(text):
            raise ValidationError(f"insert at {op.pos} is past the end of a {len(text)}-char document")
        return text[: op.pos] + op.text + text[op.pos :]
    if op.end > len(text):
        raise ValidationError(f"delete of {op.pos}..{op.end} exceeds a {len(text)}-char document")
    return text[: op.pos] + text[op.end :]


# --8<-- [end:ops]


# --8<-- [start:transform]
def transform(op: Op, against: Op, op_first: bool = False) -> Op | None:
    """Rewrite ``op`` for a document that already has ``against`` applied.

    ``op_first`` breaks the tie when both ops insert at the same offset: exactly one of the two
    mirrored calls passes ``True``, otherwise the two sites end up with the characters in a
    different order. The server passes ``False`` (the op already in the log wins); a client
    rebasing an incoming op against its own in-flight op passes ``True`` for the incoming one.

    Returns ``None`` when nothing is left to do, because the text this op touched is already gone.
    """
    match op, against:
        case Insert(), Insert():
            after = op.pos > against.pos or (op.pos == against.pos and not op_first)
            return Insert(op.pos + against.length, op.text) if after else op
        case Insert(), Delete():
            if op.pos <= against.pos:
                return op  # before the deleted range: unaffected
            if op.pos >= against.end:
                return Insert(op.pos - against.length, op.text)  # after it: shift left
            return None  # the anchor itself was deleted, so the delete wins and the text is lost
        case Delete(), Insert():
            if against.pos <= op.pos:
                return Delete(op.pos + against.length, op.length)  # inserted before us: shift right
            if against.pos >= op.end:
                return op  # inserted after us: unaffected
            return Delete(op.pos, op.length + against.length)  # inserted inside: swallow it
        case Delete(), Delete():
            if against.end <= op.pos:
                return Delete(op.pos - against.length, op.length)
            if op.end <= against.pos:
                return op
            overlap = min(op.end, against.end) - max(op.pos, against.pos)
            remaining = op.length - overlap
            if remaining == 0:
                return None  # the other delete already covered everything this one wanted
            return Delete(min(op.pos, against.pos), remaining)
    raise ValidationError(f"cannot transform {type(op).__name__} against {type(against).__name__}")


# --8<-- [end:transform]


# --8<-- [start:server]
@dataclass(frozen=True, slots=True)
class AppliedOp:
    """One entry of the authoritative log. ``op`` is ``None`` when the op transformed away."""

    revision: int
    client_id: str
    op: Op | None


class DocumentServer:
    """The single sequencer for one document: it owns the text, the log and the revision counter.

    A client submits an op together with the revision it was written against. The server rebases
    it over every op accepted since -- in log order, which *is* the total order -- applies the
    result, and appends it at revision ``len(log) + 1``. This is why a document is routed to
    exactly one server by ``document_id``: the loop below is only correct if one process decides
    the order. ``_lock`` protects ``_text`` and ``_log``.
    """

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._log: list[AppliedOp] = []
        self._lock = threading.Lock()

    @property
    def text(self) -> str:
        with self._lock:
            return self._text

    @property
    def revision(self) -> int:
        with self._lock:
            return len(self._log)

    def snapshot(self) -> tuple[int, str]:
        """What a joining client loads: the text and the revision it corresponds to.

        Production writes one of these every few hundred ops, so opening a document replays a
        short tail of the log instead of five years of keystrokes.
        """
        with self._lock:
            return len(self._log), self._text

    def ops_since(self, revision: int) -> list[AppliedOp]:
        with self._lock:
            if not 0 <= revision <= len(self._log):
                raise ValidationError(f"revision {revision} is outside 0..{len(self._log)}")
            return self._log[revision:]

    def submit(self, client_id: str, op: Op, base_revision: int) -> AppliedOp:
        """Rebase, apply, append. The returned revision is what the client acknowledges."""
        with self._lock:
            if not 0 <= base_revision <= len(self._log):
                raise ValidationError(f"base revision {base_revision} is outside 0..{len(self._log)}")
            rebased: Op | None = op
            for entry in self._log[base_revision:]:
                if rebased is None:
                    break
                if entry.op is not None:
                    rebased = transform(rebased, entry.op, op_first=False)
            if rebased is not None:
                self._text = apply_op(self._text, rebased)
            applied = AppliedOp(len(self._log) + 1, client_id, rebased)
            self._log.append(applied)  # a transformed-away op still consumes a revision
            return applied


# --8<-- [end:server]


# --8<-- [start:client]
class ClientDocument:
    """One editor: optimistic local apply, one op in flight, and the rebase that keeps it honest.

    Real clients compose further keystrokes into a buffer while an op is outstanding; composing
    two ops into one is separate machinery, so this model allows a single op in flight and says so
    rather than pretending. ``push`` sends the op with the revision it was written against;
    ``pull`` replays the log -- other people's ops first, then this client's own op as history.
    """

    def __init__(self, client_id: str, text: str = "", revision: int = 0) -> None:
        self.client_id = client_id
        self.text = text
        self.revision = revision
        self._outstanding: Op | None = None
        self._sent = False

    @property
    def outstanding(self) -> Op | None:
        return self._outstanding

    def edit(self, op: Op) -> None:
        """Apply locally at once: a keystroke must never wait for a round trip."""
        if self._outstanding is not None:
            raise InvalidStateError("one op in flight; pull the acknowledgement before editing again")
        self.text = apply_op(self.text, op)
        self._outstanding = op

    def push(self, server: DocumentServer) -> bool:
        """Send the outstanding op. False when there is nothing to send or it is already in flight."""
        if self._outstanding is None or self._sent:
            return False
        server.submit(self.client_id, self._outstanding, self.revision)
        self._sent = True
        return True

    def pull(self, server: DocumentServer) -> None:
        """Replay everything accepted since our revision.

        A remote op was written without knowledge of our outstanding op, so both are transformed:
        the remote one to apply here, ours to match what the server already computed. The tie-break
        mirrors the server's -- the op already in the log wins, so ``op_first=True`` for the remote.
        """
        for applied in server.ops_since(self.revision):
            self.revision = applied.revision
            if applied.client_id == self.client_id:
                self._outstanding, self._sent = None, False  # our op is history now
                continue
            remote = applied.op
            if remote is not None and self._outstanding is not None:
                remote, self._outstanding = (
                    transform(remote, self._outstanding, op_first=True),
                    transform(self._outstanding, remote, op_first=False),
                )
            if remote is not None:
                self.text = apply_op(self.text, remote)


# --8<-- [end:client]


def main() -> None:
    server = DocumentServer("hello world")
    revision, text = server.snapshot()
    alice = ClientDocument("alice", text, revision)
    bob = ClientDocument("bob", text, revision)
    print(f'revision {revision}: "{text}"')

    alice.edit(Insert(6, "big "))
    bob.edit(Insert(6, "cruel "))  # same offset, neither client knows about the other
    print(f'alice types at offset 6 -> "{alice.text}"')
    print(f'bob types at offset 6   -> "{bob.text}"')

    alice.push(server)
    bob.push(server)
    print(f"server rebased bob's op to {server.ops_since(1)[0].op}")
    alice.pull(server)
    bob.pull(server)
    print(f'server: "{server.text}"')
    print(f'alice:  "{alice.text}"')
    print(f'bob:    "{bob.text}"')
    print("converged:", server.text == alice.text == bob.text, "at revision", server.revision)

    revision, text = server.snapshot()
    carol = ClientDocument("carol", text, revision)
    dave = ClientDocument("dave", text, revision)
    carol.edit(Delete(6, 10))  # remove "big cruel "
    dave.edit(Insert(10, "very "))  # types inside the range carol is deleting
    carol.push(server)
    dave.push(server)
    carol.pull(server)
    dave.pull(server)
    print(f'after the overlapping delete: server "{server.text}", dave "{dave.text}"')
    print("dave's 'very ' was swallowed by the delete: one op in, zero ops out")
    print("log:", [(a.revision, a.client_id, a.op) for a in server.ops_since(2)])


if __name__ == "__main__":
    main()
