"""Entries, the intrusive list the policies share, stats and domain errors.

Nothing here knows about locks or eviction rules: ``policies.py`` owns the order
and ``services.py`` owns the cache, the lock and the counters.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from enum import StrEnum

from common import InvalidStateError, NotFoundError, ValidationError


# --8<-- [start:errors]
class CapacityError(ValidationError):
    """Capacity is not a positive integer, so the cache could never hold anything."""


class KeyMissingError(NotFoundError):
    """Strict lookup (``cache[key]``) for a key that is absent or already expired."""


class EmptyCacheError(InvalidStateError):
    """A policy was asked for a victim while it was tracking no keys at all."""


class EvictionPolicyName(StrEnum):
    LRU = "lru"
    LFU = "lfu"
    FIFO = "fifo"


# --8<-- [end:errors]


# --8<-- [start:entry]
@dataclass(frozen=True, slots=True)
class Entry[V]:
    """One stored value plus its deadline. Frozen: a rewrite replaces the entry.

    ``expires_at`` is an absolute instant read from the injected ``Clock``, not a
    remaining duration, so nothing has to be decremented as time passes.
    """

    value: V
    expires_at: float | None = None

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def ttl_remaining(self, now: float) -> float | None:
        return None if self.expires_at is None else max(0.0, self.expires_at - now)


@dataclass(frozen=True, slots=True)
class CacheStats:
    """An immutable snapshot of the counters; the cache never hands out live state."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0
    loads: int = 0
    coalesced: int = 0
    size: int = 0
    capacity: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_ratio(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def __str__(self) -> str:
        return (
            f"size={self.size}/{self.capacity} hits={self.hits} misses={self.misses} "
            f"evictions={self.evictions} expirations={self.expirations} "
            f"hit_ratio={self.hit_ratio:.2f}"
        )


# --8<-- [end:entry]


# --8<-- [start:list]
@dataclass(slots=True)
class Node:
    """A key inside an intrusive doubly linked list.

    Intrusive means the list stores no wrapper objects: the policy keeps
    ``dict[key, Node]`` and every operation is pointer surgery on the node it
    already has, which is what turns "move this key to the front" into O(1).
    ``freq`` is used by ``LFUPolicy`` only; one node type serves both policies.
    """

    key: Hashable
    freq: int = 1
    prev: Node | None = None
    next: Node | None = None


class DoublyLinkedList:
    """Insertion order with O(1) push, unlink and pop. Front is newest, back is the victim.

    The two sentinel nodes are the detail interviewers watch for: with a permanent
    head and tail, ``push_front`` and ``unlink`` never test for ``None``, so there
    is no special case for the first or the last element to get wrong.
    """

    def __init__(self) -> None:
        self._head = Node(key=None)  # sentinel before the newest node
        self._tail = Node(key=None)  # sentinel after the oldest node
        self._head.next = self._tail
        self._tail.prev = self._head
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def is_empty(self) -> bool:
        return self._size == 0

    def push_front(self, node: Node) -> None:
        first = self._head.next
        node.prev, node.next = self._head, first
        self._head.next = node
        first.prev = node  # type: ignore[union-attr]
        self._size += 1

    def unlink(self, node: Node) -> None:
        before, after = node.prev, node.next
        before.next = after  # type: ignore[union-attr]
        after.prev = before  # type: ignore[union-attr]
        node.prev = node.next = None
        self._size -= 1

    def move_to_front(self, node: Node) -> None:
        self.unlink(node)
        self.push_front(node)

    def pop_back(self) -> Node:
        """Remove and return the oldest node - the eviction victim."""
        if self.is_empty():
            raise EmptyCacheError("the list is empty; there is nothing to evict")
        victim = self._tail.prev
        self.unlink(victim)  # type: ignore[arg-type]
        return victim  # type: ignore[return-value]

    def keys_back_to_front(self) -> list[Hashable]:
        """Keys in eviction order, victim first. O(n); used by tests and the demo."""
        out: list[Hashable] = []
        node = self._tail.prev
        while node is not None and node is not self._head:
            out.append(node.key)
            node = node.prev
        return out


# --8<-- [end:list]
