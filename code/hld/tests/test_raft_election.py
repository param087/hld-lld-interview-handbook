import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import InvalidStateError, NotFoundError, ValidationError
from hld.raft_election import (
    AppendEntries,
    AppendReply,
    LogEntry,
    RaftCluster,
    RequestVote,
    Role,
    SimulationTimeout,
    VoteReply,
)

IDS = ["n1", "n2", "n3", "n4", "n5"]


def logs_match(cluster: RaftCluster) -> bool:
    """Log Matching: same index and term on two nodes means identical logs up to that index."""
    nodes = list(cluster.nodes.values())
    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            for index in range(min(len(a.log), len(b.log)), 0, -1):
                if a.log[index - 1].term == b.log[index - 1].term:
                    if a.log[:index] != b.log[:index]:
                        return False
                    break
    return True


def committed_prefixes_agree(cluster: RaftCluster) -> bool:
    """State Machine Safety: no two nodes ever commit different commands at one index."""
    committed = [node.committed() for node in cluster.nodes.values()]
    return all(
        a[: min(len(a), len(b))] == b[: min(len(a), len(b))] for a in committed for b in committed
    )


def run_with_random_faults(seed: int, ms: int = 3_000) -> RaftCluster:
    """Partitions, crashes, restarts and client writes drawn from a second seeded RNG."""
    cluster = RaftCluster(IDS, seed=seed)
    chaos = random.Random(seed * 7919)
    for round_no in range(ms // 100):
        cluster.run(100)
        roll = chaos.random()
        if roll < 0.15:
            cluster.partition(chaos.sample(IDS, chaos.randint(1, 2)))
        elif roll < 0.30:
            cluster.heal()
        elif roll < 0.45:
            victim = chaos.choice(IDS)
            if cluster.nodes[victim].alive:
                cluster.crash(victim)
        elif roll < 0.60:
            for node_id in IDS:
                if not cluster.nodes[node_id].alive:
                    cluster.restart(node_id)
        elif cluster.leader() is not None:
            cluster.submit(f"cmd-{round_no}")
    return cluster


def test_one_leader_is_elected_and_heartbeats_keep_followers_quiet() -> None:
    cluster = RaftCluster(IDS, seed=42)
    leader = cluster.run_until_leader(max_ms=1_000)
    vote_requests = cluster.sent["RequestVote"]
    cluster.run(1_000)
    assert cluster.leaders_by_term == {1: [leader]}
    assert cluster.sent["RequestVote"] == vote_requests  # no follower's timer fired
    followers = [node for node in cluster.nodes.values() if node.id != leader]
    assert all(node.role is Role.FOLLOWER and node.leader_id == leader for node in followers)
    assert all(node.term == 1 for node in cluster.nodes.values())
    assert cluster.sent["AppendEntries"] >= 4 * (1_000 // cluster.heartbeat_ms)


@pytest.mark.parametrize("seed", range(1, 21))
def test_at_most_one_leader_per_term_under_random_partitions_and_crashes(seed: int) -> None:
    cluster = run_with_random_faults(seed)
    assert cluster.election_safety_holds(), dict(cluster.leaders_by_term)
    assert all(len(ids) == 1 for ids in cluster.leaders_by_term.values()), dict(cluster.leaders_by_term)
    assert logs_match(cluster)
    assert committed_prefixes_agree(cluster)
    cluster.heal()
    for node_id in IDS:
        if not cluster.nodes[node_id].alive:
            cluster.restart(node_id)
    cluster.run_until_leader(max_ms=3_000)  # liveness returns once the network does
    cluster.run(300)  # heartbeats stop every other timer, so leadership settles
    leader = cluster.leader()
    assert leader is not None
    # Raft does not erase a follower's uncommitted tail until the leader overwrites that index,
    # so convergence is proved by one fresh write, not by waiting longer.
    cluster.submit(f"post-heal-{seed}", leader)
    cluster.run(700)
    final = cluster.leader()
    assert final is not None
    assert all(node.log == cluster.nodes[final].log for node in cluster.nodes.values())
    assert all(node.committed() == cluster.nodes[final].committed() for node in cluster.nodes.values())
    assert cluster.election_safety_holds()


def test_leader_crash_triggers_a_new_term_and_the_old_leader_rejoins_as_follower() -> None:
    cluster = RaftCluster(IDS, seed=7)
    first = cluster.run_until_leader(max_ms=1_000)
    cluster.crash(first)
    elapsed = cluster.run_until(lambda: cluster.leader() is not None, max_ms=2_000)
    second = cluster.leader()
    assert second is not None and second != first
    assert cluster.nodes[second].term == 2
    assert elapsed <= 2 * 300 + 3 * 15  # two timeouts plus a few message delays
    cluster.restart(first)
    cluster.run(200)
    old = cluster.nodes[first]
    assert old.role is Role.FOLLOWER and old.term == 2 and old.leader_id == second
    assert cluster.election_safety_holds()


def test_stale_leader_in_a_minority_cannot_commit_and_is_overwritten_after_the_heal() -> None:
    cluster = RaftCluster(IDS, seed=42)
    leader = cluster.run_until_leader(max_ms=1_000)
    cluster.submit("a")
    cluster.run(100)
    assert cluster.nodes[leader].committed() == ["a"]
    buddy = next(node_id for node_id in IDS if node_id != leader)
    cluster.partition([leader, buddy])
    cluster.run_until(lambda: cluster.leader() != leader, max_ms=3_000)
    new_leader = cluster.leader()
    assert new_leader is not None and new_leader not in (leader, buddy)
    cluster.submit("stale", leader)
    cluster.submit("fresh", new_leader)
    cluster.run(200)
    old, new = cluster.nodes[leader], cluster.nodes[new_leader]
    assert old.role is Role.LEADER and old.committed() == ["a"]  # accepted, never committed
    assert [entry.command for entry in old.log] == ["a", "stale"]
    assert new.committed() == ["a", "fresh"]
    cluster.heal()
    cluster.run(400)
    assert old.role is Role.FOLLOWER and old.term == new.term
    assert all(node.committed() == ["a", "fresh"] for node in cluster.nodes.values())
    assert cluster.election_safety_holds()


def test_votes_go_only_to_up_to_date_candidates_and_only_once_per_term() -> None:
    cluster = RaftCluster(IDS, seed=1)
    voter = cluster.nodes["n1"]
    voter.term = 1
    voter.log = [LogEntry(1, "a"), LogEntry(1, "b")]
    behind = voter.on_message("n2", RequestVote(term=2, candidate="n2", last_log_index=1, last_log_term=1))
    assert behind == VoteReply(2, False)  # shorter log in the same last term: refused
    assert voter.term == 2 and voter.voted_for is None  # but the higher term was adopted
    older = voter.on_message("n3", RequestVote(term=2, candidate="n3", last_log_index=9, last_log_term=0))
    assert older == VoteReply(2, False)  # longer log but older last term: refused
    ok = voter.on_message("n4", RequestVote(term=2, candidate="n4", last_log_index=2, last_log_term=1))
    assert ok == VoteReply(2, True)
    again = voter.on_message("n4", RequestVote(term=2, candidate="n4", last_log_index=2, last_log_term=1))
    assert again == VoteReply(2, True)  # the same candidate may be re-granted (lost reply)
    rival = voter.on_message("n5", RequestVote(term=2, candidate="n5", last_log_index=5, last_log_term=2))
    assert rival == VoteReply(2, False)  # one vote per term, however good the rival's log
    stale = voter.on_message("n5", RequestVote(term=1, candidate="n5", last_log_index=5, last_log_term=2))
    assert stale == VoteReply(2, False)  # an old term is always refused and told the new term


def test_append_entries_consistency_check_and_conflicting_tail_truncation() -> None:
    cluster = RaftCluster(IDS, seed=1)
    follower = cluster.nodes["n1"]
    follower.log = [LogEntry(1, "a"), LogEntry(1, "b"), LogEntry(2, "c")]
    follower.term = 2
    gap = follower.on_message(
        "n2", AppendEntries(3, "n2", prev_log_index=5, prev_log_term=3, entries=(), leader_commit=1)
    )
    assert gap == AppendReply(3, False, 0)
    assert follower.role is Role.FOLLOWER and follower.leader_id == "n2" and follower.term == 3
    mismatch = follower.on_message(
        "n2", AppendEntries(3, "n2", prev_log_index=3, prev_log_term=3, entries=(), leader_commit=1)
    )
    assert mismatch == AppendReply(3, False, 0)
    assert [entry.command for entry in follower.log] == ["a", "b", "c"]  # untouched so far
    fixed = follower.on_message(
        "n2",
        AppendEntries(3, "n2", prev_log_index=1, prev_log_term=1, entries=(LogEntry(3, "d"),), leader_commit=2),
    )
    assert fixed == AppendReply(3, True, 2)
    assert follower.log == [LogEntry(1, "a"), LogEntry(3, "d")]
    assert follower.committed() == ["a", "d"]
    heartbeat = follower.on_message(
        "n2", AppendEntries(3, "n2", prev_log_index=2, prev_log_term=3, entries=(), leader_commit=9)
    )
    assert heartbeat == AppendReply(3, True, 2)
    assert follower.commit_index == 2  # never beyond what this node actually holds


def test_commit_index_advances_only_for_a_majority_of_current_term_entries() -> None:
    cluster = RaftCluster(IDS, seed=1)
    leader = cluster.run_until_leader(max_ms=1_000)
    node = cluster.nodes[leader]
    node.log = [LogEntry(node.term - 1, "old")] if node.term > 1 else node.log  # keep it simple
    node.term += 1  # pretend this node won a later term with an uncommitted older entry
    node.log = [LogEntry(node.term - 1, "old")]
    node.commit_index = 0
    peers = list(node.peers)
    node.handle_append_reply(peers[0], AppendReply(node.term, True, 1))
    node.handle_append_reply(peers[1], AppendReply(node.term, True, 1))
    assert node.commit_index == 0  # 3/5 hold it, but it is not from the current term (Figure 8)
    node.submit("new")
    node.handle_append_reply(peers[0], AppendReply(node.term, True, 2))
    assert node.commit_index == 0  # 2/5 hold the current-term entry
    node.handle_append_reply(peers[1], AppendReply(node.term, True, 2))
    assert node.commit_index == 2  # majority on a current-term entry commits it and the old one
    newer_term = node.term + 1
    node.on_message(peers[2], AppendReply(newer_term, True, 2))  # a reply reveals a newer term
    assert node.role is Role.FOLLOWER and node.term == newer_term and node.voted_for is None
    assert node.commit_index == 2  # already-committed entries survive the step-down


def test_identical_timeouts_split_votes_forever_while_random_timeouts_converge() -> None:
    fixed = RaftCluster(IDS, seed=42, election_timeout_ms=(200, 200))
    fixed.run(2_000)
    assert fixed.leader() is None and not fixed.leaders_by_term
    assert all(node.term == 10 for node in fixed.nodes.values())  # a new split vote every 200 ms
    with pytest.raises(SimulationTimeout):
        fixed.run_until_leader(max_ms=1_000)
    randomized = RaftCluster(IDS, seed=42)
    assert randomized.run_until_leader(max_ms=1_000) is not None
    assert max(randomized.leaders_by_term) <= 2


def test_the_same_seed_reproduces_the_same_run_in_parallel() -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(run_with_random_faults, [3, 3, 3, 3]))
    assert all(run.events == runs[0].events for run in runs)
    assert all(dict(run.leaders_by_term) == dict(runs[0].leaders_by_term) for run in runs)
    assert run_with_random_faults(4).events != runs[0].events


def test_validation_and_state_errors() -> None:
    with pytest.raises(ValidationError):
        RaftCluster([])
    with pytest.raises(ValidationError):
        RaftCluster(["a", "a"])
    with pytest.raises(ValidationError):
        RaftCluster(IDS, election_timeout_ms=(300, 150))
    with pytest.raises(ValidationError):
        RaftCluster(IDS, heartbeat_ms=150)
    with pytest.raises(ValidationError):
        RaftCluster(IDS, latency_ms=(10, 5))
    cluster = RaftCluster(IDS, seed=1)
    with pytest.raises(InvalidStateError):
        cluster.submit("x")  # nobody is leader yet
    with pytest.raises(InvalidStateError):
        cluster.submit("x", "n1")
    with pytest.raises(NotFoundError):
        cluster.crash("n9")
    with pytest.raises(ValidationError):
        cluster.partition(["n1", "n9"])
