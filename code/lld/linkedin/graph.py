"""The connection graph: an undirected adjacency map plus a depth-limited BFS.

Everything about degrees is here, and nothing about requests, privacy or feeds.
Keeping the graph ignorant of *why* an edge exists is what lets you swap it for
a real graph store later without touching a service.
"""

from __future__ import annotations

import threading
from collections import deque

from lld.linkedin.models import SelfConnectionError


# --8<-- [start:graph]
class ConnectionGraph:
    """Undirected, in memory, guarded by one lock.

    ``_lock`` protects the adjacency map. Every traversal is bounded by
    ``max_depth`` (3 by default) and by ``NODE_BUDGET``, so a read can never
    hold the lock while walking a whole network — the interview answer for
    "what if someone has 30,000 connections".
    """

    MAX_DEGREE = 3
    NODE_BUDGET = 50_000

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._edges: dict[str, set[str]] = {}
        self._follows: dict[str, set[str]] = {}

    # -- writes ---------------------------------------------------------------
    def add_edge(self, a: str, b: str) -> bool:
        """Connect two members both ways. Returns False if the edge already existed."""
        if a == b:
            raise SelfConnectionError("a member cannot connect to themselves")
        first, second = sorted((a, b))  # fixed order, so nested calls cannot deadlock
        with self._lock:
            if second in self._edges.get(first, ()):
                return False
            self._edges.setdefault(first, set()).add(second)
            self._edges.setdefault(second, set()).add(first)
            return True

    def remove_edge(self, a: str, b: str) -> bool:
        with self._lock:
            if b not in self._edges.get(a, ()):
                return False
            self._edges[a].discard(b)
            self._edges[b].discard(a)
            return True

    def follow(self, follower_id: str, target_id: str) -> None:
        if follower_id == target_id:
            raise SelfConnectionError("a member cannot follow themselves")
        with self._lock:
            self._follows.setdefault(follower_id, set()).add(target_id)

    # -- reads ----------------------------------------------------------------
    def connections(self, member_id: str) -> set[str]:
        with self._lock:
            return set(self._edges.get(member_id, ()))

    def following(self, member_id: str) -> set[str]:
        with self._lock:
            return set(self._follows.get(member_id, ()))

    def are_connected(self, a: str, b: str) -> bool:
        with self._lock:
            return b in self._edges.get(a, ())

    def edge_count(self) -> int:
        with self._lock:
            return sum(len(peers) for peers in self._edges.values()) // 2

    def degree(self, source: str, target: str, max_depth: int = MAX_DEGREE) -> int | None:
        """BFS distance in hops, or None beyond ``max_depth``.

        1 is a direct connection, 2 a friend of a friend, 3 the edge of the
        network. The depth limit is not an optimisation: on a social graph the
        fourth hop reaches most of the population, so it would answer nothing.
        """
        if source == target:
            return 0
        with self._lock:
            seen = {source}
            frontier = deque([(source, 0)])
            while frontier:
                node, depth = frontier.popleft()
                if depth >= max_depth or len(seen) > self.NODE_BUDGET:
                    continue
                for peer in self._edges.get(node, ()):
                    if peer == target:
                        return depth + 1
                    if peer not in seen:
                        seen.add(peer)
                        frontier.append((peer, depth + 1))
        return None

    def within(self, source: str, max_depth: int = MAX_DEGREE) -> dict[str, int]:
        """Everyone reachable within ``max_depth``, mapped to their degree."""
        found: dict[str, int] = {}
        with self._lock:
            frontier = deque([(source, 0)])
            visited = {source}
            while frontier:
                node, depth = frontier.popleft()
                if depth >= max_depth:
                    continue
                for peer in self._edges.get(node, ()):
                    if peer in visited:
                        continue
                    visited.add(peer)
                    found[peer] = depth + 1
                    frontier.append((peer, depth + 1))
        return found

    def mutual(self, a: str, b: str) -> set[str]:
        with self._lock:
            return set(self._edges.get(a, ())) & set(self._edges.get(b, ()))

    def people_you_may_know(self, member_id: str, limit: int = 5) -> list[tuple[str, int]]:
        """Second-degree members, most mutual connections first, then by id."""
        direct = self.connections(member_id)
        candidates = {
            other: len(self.mutual(member_id, other))
            for other, degree in self.within(member_id, 2).items()
            if degree == 2 and other not in direct
        }
        return sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


# --8<-- [end:graph]
