"""Email threading: group messages into conversations with union-find over RFC 5322 headers.

The crux of the Gmail design in one module:

* every message carries ``Message-ID``, and replies carry ``In-Reply-To`` plus the whole
  ancestor chain in ``References`` -- so threading is a *connected-components* problem,
  not a tree walk;
* messages arrive out of order (you are copied on a reply before you get the original), so
  the index unions against ids it has never seen -- "ghost" nodes that a later delivery fills in;
* clients strip headers, so a normalised-subject fallback rescues orphaned replies;
* union by size means a merge rewrites the *smaller* half of the thread, which is exactly
  the property that keeps the mailbox write bounded when two half-threads join.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from common import NotFoundError, ValidationError


# --8<-- [start:models]
@dataclass(frozen=True, slots=True)
class Message:
    """One delivered message; the fields a threading index actually reads."""

    message_id: str
    from_addr: str
    subject: str
    received_at: float
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()

    def parent_ids(self) -> tuple[str, ...]:
        """Every ancestor this message claims, oldest first, deduplicated (RFC 5322 3.6.4).

        ``References`` is the ancestor chain and ``In-Reply-To`` is the immediate parent;
        clients disagree about which they populate, so accept both and dedupe.
        """
        seen: dict[str, None] = {}
        for mid in (*self.references, self.in_reply_to):
            if mid and mid != self.message_id:
                seen[mid] = None
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class Thread:
    """A conversation as the mailbox list view renders it."""

    thread_id: str
    subject: str
    message_ids: tuple[str, ...]
    participants: tuple[str, ...]
    updated_at: float

    @property
    def size(self) -> int:
        return len(self.message_ids)


# --8<-- [end:models]


# --8<-- [start:union_find]
class UnionFind:
    """Disjoint sets over message ids: path compression plus union by size.

    Both operations are near O(1) amortised, which is what lets the mailbox service thread a
    message *on arrival* instead of re-reading the conversation. ``_members`` is merged
    small-into-large, so the total merge work over n messages is O(n log n).
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._members: dict[str, list[str]] = {}  # root -> ids, only roots are keys

    def add(self, key: str) -> None:
        if key not in self._parent:
            self._parent[key] = key
            self._members[key] = [key]

    def find(self, key: str) -> str:
        self.add(key)  # unknown id (a reference to a message we have not received): ghost node
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:  # path compression on the way back down
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, left: str, right: str) -> str:
        """Merge two sets and return the surviving root (the larger side)."""
        a, b = self.find(left), self.find(right)
        if a == b:
            return a
        if len(self._members[a]) < len(self._members[b]):
            a, b = b, a
        self._parent[b] = a
        self._members[a].extend(self._members.pop(b))
        return a

    def members(self, key: str) -> list[str]:
        return list(self._members[self.find(key)])

    def roots(self) -> list[str]:
        return list(self._members)


# --8<-- [end:union_find]


# --8<-- [start:threader]
SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|aw|sv)\s*(?:\[\d+\])?\s*:\s*)+", re.IGNORECASE)


def normalise_subject(subject: str) -> str:
    """Strip any run of reply/forward prefixes: 'Re: Re: Fwd: Ship it' -> 'ship it'."""
    return SUBJECT_PREFIX.sub("", subject).strip().casefold()


class MailboxThreader:
    """The threading index of one mailbox; in production this is a partition, not a process.

    ``add()`` is idempotent because SMTP is at-least-once: a retried delivery of the same
    ``Message-ID`` must not duplicate a message inside its thread.
    """

    def __init__(self, *, merge_by_subject: bool = True) -> None:
        self._sets = UnionFind()
        self._messages: dict[str, Message] = {}
        self._subject_seed: dict[str, str] = {}  # normalised subject -> any id in that thread
        self._merge_by_subject = merge_by_subject
        self._lock = threading.RLock()  # guards _sets, _messages and _subject_seed together

    def add(self, message: Message) -> str:
        """Index one delivered message and return the id of the thread it landed in."""
        if not message.message_id.strip():
            raise ValidationError("Message-ID is mandatory (RFC 5322 3.6.4)")
        with self._lock:
            if message.message_id in self._messages:
                return self._thread_id(message.message_id)  # redelivery: at-least-once, deduped
            self._messages[message.message_id] = message
            root = self._sets.find(message.message_id)
            for parent in message.parent_ids():
                root = self._sets.union(root, parent)  # a missing parent becomes a ghost node
            if self._merge_by_subject and (key := normalise_subject(message.subject)):
                seed = self._subject_seed.get(key)
                if seed is not None and self._looks_like_reply(message):
                    root = self._sets.union(root, seed)  # headers were stripped by the client
                self._subject_seed.setdefault(key, message.message_id)
            return self._thread_id(root)

    @staticmethod
    def _looks_like_reply(message: Message) -> bool:
        """Only replies may be merged on subject alone; two fresh 'Hello' mails must not."""
        return bool(message.parent_ids()) or SUBJECT_PREFIX.match(message.subject) is not None

    def thread_of(self, message_id: str) -> Thread:
        with self._lock:
            if message_id not in self._messages:
                raise NotFoundError(f"unknown message {message_id!r}")
            return self._build(message_id)

    def threads(self) -> list[Thread]:
        """Every thread, newest activity first -- the mailbox list view."""
        with self._lock:
            seeds = {self._sets.find(mid) for mid in self._messages}
            built = [self._build(seed) for seed in seeds]
        return sorted(built, key=lambda t: (-t.updated_at, t.thread_id))

    def ghost_ids(self) -> set[str]:
        """Referenced ids we have never received: replies whose parent went to someone else."""
        with self._lock:
            return {mid for root in self._sets.roots() for mid in self._sets.members(root)} - set(
                self._messages
            )

    def _delivered(self, key: str) -> list[Message]:
        """Messages of this set in thread order; the caller holds the lock."""
        known = [self._messages[mid] for mid in self._sets.members(key) if mid in self._messages]
        known.sort(key=lambda m: (m.received_at, m.message_id))
        return known

    def _thread_id(self, key: str) -> str:
        """The oldest delivered message names the thread, so a merge keeps the older id."""
        return self._delivered(key)[0].message_id

    def _build(self, key: str) -> Thread:
        known = self._delivered(key)
        return Thread(
            thread_id=known[0].message_id,
            subject=known[0].subject,
            message_ids=tuple(m.message_id for m in known),
            participants=tuple(dict.fromkeys(m.from_addr for m in known)),
            updated_at=known[-1].received_at,
        )


# --8<-- [end:threader]


def main() -> None:
    index = MailboxThreader()
    inbox = [
        Message("<a1@corp>", "ana@corp.example", "Ship the release", 100.0),
        # Cara replies to Bob's mail, which this mailbox has not received yet.
        Message(
            "<c1@corp>",
            "cara@corp.example",
            "Re: Ship the release",
            102.0,
            in_reply_to="<b1@corp>",
            references=("<a1@corp>", "<b1@corp>"),
        ),
        Message(
            "<b1@corp>",
            "bob@corp.example",
            "Re: Ship the release",
            103.0,
            in_reply_to="<a1@corp>",
            references=("<a1@corp>",),
        ),
        # Dan's client dropped In-Reply-To and References; only the subject rescues it.
        Message("<d1@corp>", "dan@corp.example", "RE: Ship the release", 104.0),
        Message("<e1@corp>", "eve@corp.example", "Q3 budget", 105.0),
    ]
    for message in inbox:
        thread_id = index.add(message)
        ghosts = sorted(index.ghost_ids())
        print(f"deliver {message.message_id:<11} -> thread {thread_id:<11} ghosts={ghosts}")
    print(f"redeliver <b1@corp> -> thread {index.add(inbox[2]):<11} (SMTP retry, deduped)")
    for thread in index.threads():
        print(
            f"thread {thread.thread_id:<11} {thread.size} msgs "
            f"{thread.subject!r} <- {', '.join(thread.participants)}"
        )


if __name__ == "__main__":
    main()
