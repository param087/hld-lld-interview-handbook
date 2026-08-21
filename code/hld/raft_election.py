"""Raft leader election and log replication as a deterministic discrete-event simulation.

What the module demonstrates, in the order an interviewer asks about it:

* ``RaftNode`` follows the server rules of the Raft paper (Figure 2): randomized election
  timeouts, terms, one persisted vote per term, ``RequestVote`` with the up-to-date log
  check, ``AppendEntries`` heartbeats with the log consistency check, and a commit index
  that advances only once a majority holds an entry of the leader's current term.
* ``RaftCluster`` drives the nodes one simulated millisecond per ``step`` from a ``FakeClock``
  and draws timeouts and network latency from a seeded ``random.Random``, so a run is a pure
  function of the seed and the fault schedule; ``partition``, ``heal``, ``crash`` and
  ``restart`` inject the faults.
* ``leaders_by_term`` records every election, so tests can assert Election Safety (at most
  one leader per term) under any schedule of partitions and crashes.
"""

from __future__ import annotations

import heapq
import itertools
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from common import FakeClock, HandbookError, InvalidStateError, NotFoundError, ValidationError


class SimulationTimeout(HandbookError):
    """``run_until`` spent its budget before the condition held: a liveness failure."""


# --8<-- [start:messages]
class Role(StrEnum):
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


@dataclass(frozen=True, slots=True)
class LogEntry:
    term: int
    command: str


@dataclass(frozen=True, slots=True)
class RequestVote:
    """Candidate to every peer; the last log position lets voters apply the election restriction."""

    term: int
    candidate: str
    last_log_index: int
    last_log_term: int


@dataclass(frozen=True, slots=True)
class VoteReply:
    term: int
    granted: bool


@dataclass(frozen=True, slots=True)
class AppendEntries:
    """Leader to every follower: a heartbeat when ``entries`` is empty, replication otherwise."""

    term: int
    leader: str
    prev_log_index: int
    prev_log_term: int
    entries: tuple[LogEntry, ...]
    leader_commit: int


@dataclass(frozen=True, slots=True)
class AppendReply:
    term: int
    success: bool
    match_index: int  # highest index the follower now knows it shares with the leader


Message = RequestVote | VoteReply | AppendEntries | AppendReply
# --8<-- [end:messages]


# --8<-- [start:election]
class RaftNode:
    """One Raft server. Indices are 1-based as in the paper: ``log[i - 1]`` is entry ``i``.

    ``term``, ``voted_for`` and ``log`` are the persistent state. A real server fsyncs them
    before answering any RPC, and ``RaftCluster.restart`` keeps them for the same reason: a
    node that forgot its vote could vote twice in one term and elect two leaders. The rest
    is volatile and rebuilt after a restart.
    """

    def __init__(self, node_id: str, peers: Sequence[str], cluster: RaftCluster) -> None:
        self.id = node_id
        self.peers = tuple(peers)
        self._cluster = cluster
        self.term = 0
        self.voted_for: str | None = None
        self.log: list[LogEntry] = []
        self.role = Role.FOLLOWER
        self.leader_id: str | None = None
        self.commit_index = 0
        self.alive = True
        self._votes: set[str] = set()
        self._next_index: dict[str, int] = {}
        self._match_index: dict[str, int] = {}
        self._heartbeat_due = 0
        self._election_deadline = 0
        self.reset_election_timer()

    @property
    def majority(self) -> int:
        return (len(self.peers) + 1) // 2 + 1

    @property
    def last_log_index(self) -> int:
        return len(self.log)

    @property
    def last_log_term(self) -> int:
        return self.log[-1].term if self.log else 0

    def committed(self) -> list[str]:
        """Commands this node may apply to its state machine."""
        return [entry.command for entry in self.log[: self.commit_index]]

    def reset_election_timer(self) -> None:
        self._election_deadline = self._cluster.now_ms + self._cluster.random_election_timeout()

    def tick(self) -> None:
        """One simulated millisecond: a leader heartbeats, everyone else watches the timer."""
        if not self.alive:
            return
        if self.role is Role.LEADER:
            if self._cluster.now_ms >= self._heartbeat_due:
                self.broadcast_append()
        elif self._cluster.now_ms >= self._election_deadline:
            self.start_election()

    def start_election(self) -> None:
        """The timer fired without a heartbeat: new term, vote for self, ask everyone else."""
        self.term += 1
        self.role = Role.CANDIDATE
        self.voted_for = self.id
        self.leader_id = None
        self._votes = {self.id}
        self.reset_election_timer()
        self._cluster.log_event(f"{self.id} election timeout, candidate in term {self.term}")
        request = RequestVote(self.term, self.id, self.last_log_index, self.last_log_term)
        for peer in self.peers:
            self._cluster.send(self.id, peer, request)
        self._maybe_become_leader()

    def become_follower(self, term: int) -> None:
        """Rule for all servers: a higher term makes this node's term, vote and role stale."""
        if term > self.term:
            self.term = term
            self.voted_for = None
        if self.role is not Role.FOLLOWER:
            self._cluster.log_event(f"{self.id} steps down, follower in term {term}")
        self.role = Role.FOLLOWER
        self.leader_id = None

    def on_message(self, src: str, msg: Message) -> Message | None:
        """Dispatch one delivered message; the reply (if any) is sent back and returned."""
        if not self.alive:
            return None
        if msg.term > self.term:
            self.become_follower(msg.term)
        reply: Message | None = None
        match msg:
            case RequestVote():
                reply = self.handle_request_vote(msg)
            case VoteReply():
                self.handle_vote_reply(src, msg)
            case AppendEntries():
                reply = self.handle_append_entries(msg)
            case AppendReply():
                self.handle_append_reply(src, msg)
        if reply is not None:
            self._cluster.send(self.id, src, reply)
        return reply

    def handle_request_vote(self, msg: RequestVote) -> VoteReply:
        """One vote per term, only for a candidate whose log is at least as up to date as ours."""
        mine = (self.last_log_term, self.last_log_index)
        up_to_date = (msg.last_log_term, msg.last_log_index) >= mine
        granted = msg.term == self.term and self.voted_for in (None, msg.candidate) and up_to_date
        if granted:
            self.voted_for = msg.candidate
            self.reset_election_timer()  # granting a vote counts as hearing from a leader
        return VoteReply(self.term, granted)

    def handle_vote_reply(self, src: str, msg: VoteReply) -> None:
        if self.role is Role.CANDIDATE and msg.term == self.term and msg.granted:
            self._votes.add(src)
            self._maybe_become_leader()

    def _maybe_become_leader(self) -> None:
        if len(self._votes) < self.majority:
            return
        self.role = Role.LEADER
        self.leader_id = self.id
        self._next_index = dict.fromkeys(self.peers, self.last_log_index + 1)
        self._match_index = dict.fromkeys(self.peers, 0)
        self._cluster.record_leader(self.term, self.id, sorted(self._votes))
        self.broadcast_append()  # the first heartbeat stops everyone else's timer
    # --8<-- [end:election]

    # --8<-- [start:replication]
    def broadcast_append(self) -> None:
        """Leader: send every follower the entries it is missing, or an empty heartbeat."""
        self._heartbeat_due = self._cluster.now_ms + self._cluster.heartbeat_ms
        for peer in self.peers:
            self._send_append(peer)

    def _send_append(self, peer: str) -> None:
        prev_index = self._next_index[peer] - 1
        prev_term = self.log[prev_index - 1].term if prev_index > 0 else 0
        entries = tuple(self.log[prev_index:])
        message = AppendEntries(self.term, self.id, prev_index, prev_term, entries, self.commit_index)
        self._cluster.send(self.id, peer, message)

    def submit(self, command: str) -> int:
        """Client write: append to the leader's log and replicate; returns the entry's index."""
        if self.role is not Role.LEADER:
            raise InvalidStateError(f"{self.id} is not the leader")
        self.log.append(LogEntry(self.term, command))
        self.broadcast_append()
        return self.last_log_index

    def handle_append_entries(self, msg: AppendEntries) -> AppendReply:
        if msg.term < self.term:
            return AppendReply(self.term, False, 0)
        self.become_follower(msg.term)  # a candidate that hears from this term's leader yields
        self.leader_id = msg.leader
        self.reset_election_timer()
        prev = msg.prev_log_index
        if prev > self.last_log_index or (prev > 0 and self.log[prev - 1].term != msg.prev_log_term):
            return AppendReply(self.term, False, 0)  # consistency check failed: leader backs up
        for offset, entry in enumerate(msg.entries):
            position = prev + offset
            if position < len(self.log) and self.log[position].term != entry.term:
                del self.log[position:]  # conflicting tail: the leader's log wins
            if position >= len(self.log):
                self.log.append(entry)
        last_new = prev + len(msg.entries)
        if msg.leader_commit > self.commit_index:
            self.commit_index = max(self.commit_index, min(msg.leader_commit, last_new))
        return AppendReply(self.term, True, last_new)

    def handle_append_reply(self, src: str, msg: AppendReply) -> None:
        if self.role is not Role.LEADER or msg.term != self.term:
            return
        if msg.success:
            self._match_index[src] = max(self._match_index[src], msg.match_index)
            self._next_index[src] = self._match_index[src] + 1
            self._advance_commit_index()
        else:
            self._next_index[src] = max(1, self._next_index[src] - 1)
            self._send_append(src)  # retry one entry earlier until the logs agree

    def _advance_commit_index(self) -> None:
        """Commit the highest index a majority holds, counting only entries of the current term
        (paper, Figure 8); older entries are committed indirectly, once a newer one is."""
        for index in range(self.last_log_index, self.commit_index, -1):
            if self.log[index - 1].term != self.term:
                return
            replicas = 1 + sum(1 for peer in self.peers if self._match_index[peer] >= index)
            if replicas >= self.majority:
                self.commit_index = index
                return
    # --8<-- [end:replication]


# --8<-- [start:cluster]
@dataclass(order=True, frozen=True, slots=True)
class Envelope:
    deliver_at: int
    seq: int
    src: str = field(compare=False)
    dst: str = field(compare=False)
    payload: Message = field(compare=False)


class RaftCluster:
    """The nodes plus a simulated network, advanced one millisecond per ``step``.

    All randomness (election timeouts, message latency) comes from ``random.Random(seed)``
    and all time from a ``FakeClock``, so two runs with the same seed and fault schedule
    produce the same events. Messages are delivered in (time, sequence) order; a message
    whose sender and receiver are in different partition groups, or whose receiver is
    crashed, is dropped at delivery time.
    """

    def __init__(
        self,
        node_ids: Sequence[str],
        seed: int = 42,
        election_timeout_ms: tuple[int, int] = (150, 300),
        heartbeat_ms: int = 50,
        latency_ms: tuple[int, int] = (5, 15),
    ) -> None:
        if not node_ids or len(set(node_ids)) != len(node_ids):
            raise ValidationError("node ids must be non-empty and distinct")
        if not 0 < election_timeout_ms[0] <= election_timeout_ms[1]:
            raise ValidationError("election timeout range must be positive and ordered")
        if not 0 < heartbeat_ms < election_timeout_ms[0]:
            raise ValidationError("heartbeat interval must be shorter than the election timeout")
        if not 0 <= latency_ms[0] <= latency_ms[1]:
            raise ValidationError("latency range must be non-negative and ordered")
        self._rng = random.Random(seed)
        self._clock = FakeClock()
        self._timeout = election_timeout_ms
        self._latency = latency_ms
        self.heartbeat_ms = heartbeat_ms
        self.now_ms = 0
        self._seq = itertools.count()
        self._inflight: list[Envelope] = []
        self._groups: list[frozenset[str]] = []
        self.events: list[str] = []
        self.sent: Counter[str] = Counter()
        self.dropped = 0
        self.leaders_by_term: dict[int, list[str]] = defaultdict(list)
        self.nodes = {nid: RaftNode(nid, [p for p in node_ids if p != nid], self) for nid in node_ids}

    # -- services the nodes call ------------------------------------------------------------
    def random_election_timeout(self) -> int:
        return self._rng.randint(*self._timeout)

    def send(self, src: str, dst: str, msg: Message) -> None:
        self.sent[type(msg).__name__] += 1
        deliver_at = self.now_ms + self._rng.randint(*self._latency)
        heapq.heappush(self._inflight, Envelope(deliver_at, next(self._seq), src, dst, msg))

    def log_event(self, text: str) -> None:
        self.events.append(f"t={self.now_ms:>5} ms  {text}")

    def record_leader(self, term: int, node_id: str, voters: list[str]) -> None:
        self.leaders_by_term[term].append(node_id)
        self.log_event(f"{node_id} wins term {term} with votes from {', '.join(voters)}")

    # -- the simulation loop ----------------------------------------------------------------
    def reachable(self, a: str, b: str) -> bool:
        return not self._groups or any(a in group and b in group for group in self._groups)

    def step(self) -> None:
        self._clock.advance(0.001)
        self.now_ms = round(self._clock.now() * 1000)
        while self._inflight and self._inflight[0].deliver_at <= self.now_ms:
            env = heapq.heappop(self._inflight)
            if self.nodes[env.dst].alive and self.reachable(env.src, env.dst):
                self.nodes[env.dst].on_message(env.src, env.payload)
            else:
                self.dropped += 1
        for node in self.nodes.values():
            node.tick()

    def run(self, ms: int) -> None:
        for _ in range(ms):
            self.step()

    def run_until(self, condition: Callable[[], bool], max_ms: int = 5_000) -> int:
        """Step until ``condition`` holds and return the elapsed ms; raise when it never does."""
        for elapsed in range(1, max_ms + 1):
            self.step()
            if condition():
                return elapsed
        raise SimulationTimeout(f"condition still false after {max_ms} ms")

    def run_until_leader(self, max_ms: int = 5_000) -> str:
        self.run_until(lambda: self.leader() is not None, max_ms)
        leader = self.leader()
        assert leader is not None
        return leader

    def leader(self) -> str | None:
        """The live leader with the highest term; a stale one may linger in a minority partition."""
        leaders = [node for node in self.nodes.values() if node.alive and node.role is Role.LEADER]
        return max(leaders, key=lambda node: node.term).id if leaders else None

    # -- fault injection --------------------------------------------------------------------
    def partition(self, *groups: Iterable[str]) -> None:
        """Split the network into ``groups``; nodes named in none of them form one more group."""
        named = [frozenset(group) for group in groups]
        unknown = frozenset().union(*named) - frozenset(self.nodes)
        if unknown:
            raise ValidationError(f"unknown nodes {sorted(unknown)}")
        rest = frozenset(self.nodes) - frozenset().union(*named)
        self._groups = named + ([rest] if rest else [])
        shown = " | ".join("{" + ", ".join(sorted(group)) + "}" for group in self._groups)
        self.log_event(f"partition {shown}")

    def heal(self) -> None:
        self._groups = []
        self.log_event("partition healed")

    def _node(self, node_id: str) -> RaftNode:
        if node_id not in self.nodes:
            raise NotFoundError(f"no node {node_id!r}")
        return self.nodes[node_id]

    def crash(self, node_id: str) -> None:
        self._node(node_id).alive = False
        self.log_event(f"{node_id} crashes")

    def restart(self, node_id: str) -> None:
        """Back from a crash with its persistent state (term, vote, log) and nothing else."""
        node = self._node(node_id)
        node.alive = True
        node.role = Role.FOLLOWER
        node.leader_id = None
        node.commit_index = 0  # volatile: re-learned from the leader's next heartbeat
        node.reset_election_timer()
        self.log_event(f"{node_id} restarts as follower in term {node.term}")

    def submit(self, command: str, node_id: str | None = None) -> int:
        """Send a client command to ``node_id`` (default: the current leader)."""
        target = node_id if node_id is not None else self.leader()
        if target is None:
            raise InvalidStateError("no leader to submit to")
        return self._node(target).submit(command)

    def election_safety_holds(self) -> bool:
        """Election Safety: at most one leader was elected in any term."""
        return all(len(set(ids)) <= 1 for ids in self.leaders_by_term.values())


# --8<-- [end:cluster]


def main() -> None:
    ids = ["n1", "n2", "n3", "n4", "n5"]
    cluster = RaftCluster(ids, seed=42)
    print("5 nodes, election timeout 150-300 ms, heartbeat every 50 ms, latency 5-15 ms, seed 42")
    shown = 0

    def flush() -> None:
        nonlocal shown
        for line in cluster.events[shown:]:
            print(line)
        shown = len(cluster.events)

    first = cluster.run_until_leader()
    flush()
    heartbeats = cluster.sent["AppendEntries"]
    votes = cluster.sent["RequestVote"]
    cluster.run(1_000)
    print(
        f"t={cluster.now_ms:>5} ms  {first} still leads term {cluster.nodes[first].term}: "
        f"{cluster.sent['AppendEntries'] - heartbeats} heartbeats sent, "
        f"{cluster.sent['RequestVote'] - votes} vote requests (timers kept being reset)"
    )
    cluster.crash(first)
    second = cluster.run_until_leader()
    cluster.restart(first)
    cluster.run(100)
    flush()
    cluster.submit("x=1")
    cluster.submit("x=2")
    cluster.run(100)
    holders = sum(1 for node in cluster.nodes.values() if node.commit_index >= 2)
    print(
        f"t={cluster.now_ms:>5} ms  x=1, x=2 submitted to {second}: "
        f"committed on {holders}/5 nodes, {first} caught up after its restart"
    )
    cluster.partition([second, first])
    cluster.run_until(lambda: cluster.leader() != second)
    third = cluster.leader()
    assert third is not None
    flush()
    cluster.submit("x=3", second)  # the stale leader accepts the write but cannot commit it
    cluster.submit("y=1", third)
    cluster.run(200)
    old, new = cluster.nodes[second], cluster.nodes[third]
    print(
        f"t={cluster.now_ms:>5} ms  {second} (term {old.term}) accepted x=3 at index 3, commit "
        f"index still {old.commit_index}: 2/5 replicas; {third} (term {new.term}) committed y=1"
    )
    cluster.heal()
    cluster.run(300)
    flush()
    logs = {nid: [entry.command for entry in node.log] for nid, node in cluster.nodes.items()}
    agree = sum(1 for log in logs.values() if log == logs[third])
    print(
        f"t={cluster.now_ms:>5} ms  {agree}/5 logs equal {logs[third]}, committed on "
        f"{sum(1 for n in cluster.nodes.values() if n.commit_index == 3)}/5; x=3 is gone"
    )
    print(
        f"leaders per term: {dict(cluster.leaders_by_term)}; "
        f"election safety holds: {cluster.election_safety_holds()}"
    )
    fixed = RaftCluster(ids, seed=42, election_timeout_ms=(200, 200))
    fixed.run(2_000)
    print(
        f"identical 200 ms timeouts instead: no leader after 2,000 ms, "
        f"{max(node.term for node in fixed.nodes.values())} terms of split votes"
    )


if __name__ == "__main__":
    main()
