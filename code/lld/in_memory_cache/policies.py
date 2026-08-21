"""Eviction policies: the one thing about a cache an interviewer always changes.

Every policy answers the same five questions - a key arrived, a key was read, a
key left, who leaves next, and what order are you in - so the cache never asks
"which policy am I holding?".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Hashable
from typing import Protocol

from common import ValidationError
from lld.in_memory_cache.models import (
    DoublyLinkedList,
    EmptyCacheError,
    EvictionPolicyName,
    Node,
)


# --8<-- [start:protocol]
class EvictionPolicy(Protocol):
    """Owns *order*, never values. The cache owns the values, never the order.

    That split is the whole design: a policy can be swapped, unit-tested on its
    own and reasoned about without touching TTL, stats or locking. Every method
    is called with the cache's lock already held, so policies need no lock.
    """

    def on_insert(self, key: Hashable) -> None:
        """A key that was not present has just been stored."""

    def on_access(self, key: Hashable) -> None:
        """A stored key was read or rewritten."""

    def on_remove(self, key: Hashable) -> None:
        """A key left by explicit delete or by expiry (not by eviction)."""

    def evict(self) -> Hashable:
        """Drop and return the victim. Raises ``EmptyCacheError`` when empty."""

    def clear(self) -> None: ...

    def keys(self) -> list[Hashable]:
        """Tracked keys in eviction order, victim first."""

    def __len__(self) -> int: ...


# --8<-- [end:protocol]


# --8<-- [start:recency]
class RecencyPolicy(ABC):
    """Shared machinery for the two order-of-arrival policies: an index and one list.

    Template Method: insert, remove, evict and the key listing are written once
    here; subclasses override exactly one hook, ``on_access``. LRU moves the node
    to the front on every read, FIFO ignores reads - which is the entire
    difference between the two algorithms, and now it is one method long.
    """

    def __init__(self) -> None:
        self._nodes: dict[Hashable, Node] = {}
        self._order = DoublyLinkedList()

    def on_insert(self, key: Hashable) -> None:
        node = Node(key=key)
        self._nodes[key] = node
        self._order.push_front(node)

    @abstractmethod
    def on_access(self, key: Hashable) -> None:
        """The one hook that separates LRU from FIFO."""

    def on_remove(self, key: Hashable) -> None:
        self._order.unlink(self._nodes.pop(key))

    def evict(self) -> Hashable:
        if not self._nodes:
            raise EmptyCacheError("no key to evict")
        victim = self._order.pop_back()
        del self._nodes[victim.key]
        return victim.key

    def clear(self) -> None:
        self._nodes.clear()
        self._order = DoublyLinkedList()

    def keys(self) -> list[Hashable]:
        return self._order.keys_back_to_front()

    def __len__(self) -> int:
        return len(self._nodes)


class LRUPolicy(RecencyPolicy):
    """Least recently used leaves first: every read moves its node to the front."""

    def on_access(self, key: Hashable) -> None:
        self._order.move_to_front(self._nodes[key])


class FIFOPolicy(RecencyPolicy):
    """Oldest insertion leaves first; reads change nothing.

    Cheaper than LRU (no pointer moves on the read path) and worse on any
    workload with re-reads, because a hot key ages out on schedule anyway.
    """

    def on_access(self, key: Hashable) -> None:
        return None


# --8<-- [end:recency]


# --8<-- [start:lfu]
class LFUPolicy:
    """Least frequently used leaves first, ties broken by least recently used.

    ``_buckets[f]`` holds every key seen exactly ``f`` times, newest at the front,
    and ``_min_freq`` is the lowest non-empty bucket. That pair is what makes
    eviction O(1): there is never a scan for the minimum count, and the tie inside
    the bucket is settled by taking its back - the LRU key of that frequency.
    """

    def __init__(self) -> None:
        self._nodes: dict[Hashable, Node] = {}
        self._buckets: dict[int, DoublyLinkedList] = {}
        self._min_freq = 0

    def on_insert(self, key: Hashable) -> None:
        node = Node(key=key, freq=1)
        self._nodes[key] = node
        self._bucket(1).push_front(node)
        self._min_freq = 1  # a brand new key is always the least used one

    def on_access(self, key: Hashable) -> None:
        node = self._nodes[key]
        self._detach(node)
        node.freq += 1
        self._bucket(node.freq).push_front(node)

    def on_remove(self, key: Hashable) -> None:
        self._detach(self._nodes.pop(key))

    def evict(self) -> Hashable:
        if not self._nodes:
            raise EmptyCacheError("no key to evict")
        victim = self._buckets[self._min_freq].pop_back()
        self._drop_empty_bucket(self._min_freq)
        del self._nodes[victim.key]
        return victim.key

    def clear(self) -> None:
        self._nodes.clear()
        self._buckets.clear()
        self._min_freq = 0

    def keys(self) -> list[Hashable]:
        out: list[Hashable] = []
        for freq in sorted(self._buckets):
            out.extend(self._buckets[freq].keys_back_to_front())
        return out

    def __len__(self) -> int:
        return len(self._nodes)

    def _bucket(self, freq: int) -> DoublyLinkedList:
        bucket = self._buckets.get(freq)
        if bucket is None:
            bucket = self._buckets[freq] = DoublyLinkedList()
        return bucket

    def _detach(self, node: Node) -> None:
        self._buckets[node.freq].unlink(node)
        self._drop_empty_bucket(node.freq)

    def _drop_empty_bucket(self, freq: int) -> None:
        """Keep ``_min_freq`` pointing at a bucket that still exists."""
        if not self._buckets[freq].is_empty():
            return
        del self._buckets[freq]
        if self._min_freq == freq:
            # O(distinct frequencies), and only on the rare paths: an explicit
            # delete, an expiry, or the promotion of the last key at the minimum.
            self._min_freq = min(self._buckets, default=0)


# --8<-- [end:lfu]


POLICIES: dict[EvictionPolicyName, type[EvictionPolicy]] = {
    EvictionPolicyName.LRU: LRUPolicy,
    EvictionPolicyName.LFU: LFUPolicy,
    EvictionPolicyName.FIFO: FIFOPolicy,
}


def make_policy(name: EvictionPolicyName | str) -> EvictionPolicy:
    """Factory so configuration ("lfu") can pick the policy; adding one edits this dict."""
    try:
        return POLICIES[EvictionPolicyName(name)]()
    except ValueError as exc:
        raise ValidationError(f"unknown eviction policy: {name!r}") from exc
