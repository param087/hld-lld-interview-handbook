---
title: Consistency, replication and isolation tables
description: Replication topologies with their latency, ceilings and conflicts, the lag anomalies and their fixes, isolation levels against the five anomalies, N/W/R quorum arithmetic, and where common systems sit on CAP and PACELC.
---
# Consistency, replication and isolation tables

## How to use this sheet

Four independent dials, one table each: who orders writes, what a reader may observe, what a transaction may observe, and what a partition does. Memorise the isolation grid verbatim — it is the one table interviewers check word for word. Every latency figure comes from the [latency sheet](latency-and-estimation.md).

## Tables

### Replication topology

| Topology | Who orders writes | Commit latency | Write ceiling | When a node dies | Conflicts |
|---|---|---|---|---|---|
| Single leader, synchronous follower | the leader | round trip to the farthest sync follower: ~0.5 ms in-datacenter, ~70 ms cross-region | one leader, ~5k-20k writes/s | one slow or dead follower blocks every write | none |
| Single leader, semi-sync (one sync follower, rest async) | the leader | local commit plus one ~0.5 ms ack | one leader, ~5k-20k writes/s | every committed write is on two nodes; failover loses nothing | none |
| Single leader, asynchronous followers | the leader | local, no wait | one leader, ~5k-20k writes/s | failover loses the tail the followers never received | none |
| Multi-leader, one per region | each region for its own writes | local ~0.5 ms instead of a 70 ms cross-region round trip | one node per region | the region keeps working through a cut link | yes: last-writer-wins, version vectors, merge or CRDT |
| Leaderless quorum | per key, by version | the W fastest replicas, tunable per request | linear with nodes | no election; a sloppy quorum rides through | yes: siblings, read repair, hinted handoff |
| Consensus group (Raft, Paxos) | the elected leader with a majority | majority round trip: ~0.5 ms in-region, ~70 ms across a US quorum | one range's leader | automatic election, no split brain | none, at the cost of coordination |

### Replication lag: what a reader sees, and the fix

| Guarantee broken | What the user sees | Fix |
|---|---|---|
| Read-your-writes | updates a profile, refreshes, sees the old one from a lagging follower | route the user's own reads to the leader briefly, or carry the write's log position and serve only from a follower that has replayed past it |
| Monotonic reads | a comment appears, then vanishes because the second refresh hit a further-behind follower | pin the session to one follower by hashing the user id, so its view only moves forward |
| Consistent prefix | the answer appears before the question, because they sit on partitions replicating at different speeds | write causally related rows to one partition, or carry causal tokens |
| Monotonic writes | a user's two writes apply out of order | one session, one ordering domain: same leader or same partition key |
| Writes-follow-reads | a reply lands before the post it answers | attach the read's version to the write |

### Consistency models

| Model | Write cost | Under partition | What a reader can observe | Build it with | Use for |
|---|---|---|---|---|---|
| Linearizable | majority round trip: ~0.5 ms in-datacenter, ~70 ms cross-region | minority side refuses | always the latest write | consensus, or one leader with synchronous replication | money, stock, unique names, leader election |
| Sequential | same write path, local reads | minority refuses writes | a consistent but possibly old prefix | an ordered log with local reads | configuration, coordination reads |
| Causal | local write plus causality metadata | keeps serving | effects never before their causes | version vectors, dependency tracking | comments and replies, collaborative state |
| Eventual plus session guarantees | local write, ~0.5 ms | keeps serving | own writes, never backwards | sticky routing or version tokens | profiles, feeds, order history |
| Eventual | local write, ~0.5 ms | keeps serving | anything not yet converged | async replication plus a merge rule | caches, counters, carts, analytics |

!!! tip "Interview tip"
    Never answer "CP" or "AP" for a whole design. Take the one or two paths that hold an invariant, make those linearizable, say what they do during a partition, and declare the rest eventual with the session guarantee its users need: "inventory decrement is linearizable per SKU because overselling breaks an invariant; the cart is eventual and always writable because a lost add is worse than a duplicate."

### Isolation levels by anomaly

| Anomaly | Read uncommitted | Read committed | Snapshot isolation (repeatable read) | Serializable |
|---|---|---|---|---|
| Dirty read | possible | prevented | prevented | prevented |
| Non-repeatable read | possible | possible | prevented | prevented |
| Phantom | possible | possible | prevented in practice | prevented |
| Lost update | possible | possible | aborted in PostgreSQL, possible in MySQL | prevented |
| Write skew | possible | possible | possible | prevented |

### The five anomalies, named precisely

| Anomaly | Definition | The example to quote |
|---|---|---|
| Dirty read | a read sees an uncommitted write | a report totals a transfer that then rolls back |
| Non-repeatable read | one row read twice inside a transaction returns two values | a balance changes mid-report |
| Phantom | a predicate query re-run inside a transaction finds rows appeared or vanished | a count of matching orders changes between two runs |
| Lost update | two read-modify-write cycles on one row; both read `qty = 1`, both write `qty = 0` | two customers own the last item |
| Write skew | two transactions read an overlapping set, each writes a different row, and a cross-row invariant breaks | two on-call doctors each see two on call and both go off call |

Defaults worth knowing: read committed in PostgreSQL, Oracle and SQL Server; PostgreSQL's repeatable read is snapshot isolation with first-committer-wins, so the loser retries; MySQL InnoDB's does not abort the second writer, so read-then-update still loses updates unless you lock. Where an invariant lives, steer the engine: `SELECT ... FOR UPDATE`, a conditional `UPDATE ... WHERE qty > 0`, or a version column.

### Quorum arithmetic (N, W, R)

| N, W, R | W + R > N | What a read returns | Fault budget | Fits |
|---|---|---|---|---|
| N=3, W=1, R=1 | no (1 + 1 = 2) | may miss the last acknowledged write | 2 replicas down either way | telemetry, best-effort counters |
| N=3, W=2, R=2 | yes (4 > 3) | the newest acknowledged write | 1 replica down either way | the default for carts and sessions |
| N=3, W=3, R=1 | yes (4 > 3) | newest, with the cheapest reads | any replica down stops writes | read-heavy, rarely written config |
| N=3, W=1, R=3 | yes (4 > 3) | newest, with the cheapest writes | any replica down stops reads | write-heavy ingest, rare reads |
| N=5, W=2, R=3 | no (2 + 3 = 5) | may be stale: the arithmetic trap | 3 down for writes, 2 for reads | nothing; use W=3 |
| N=5, W=3, R=3 | yes (6 > 5) | the newest acknowledged write | 2 replicas down either way | higher durability at higher latency |

Three caveats to say out loud: the rule covers the newest *acknowledged* write, so a write that failed with fewer than W acks is never rolled back and may still surface; a sloppy quorum satisfies W from stand-ins the read quorum never asks, destroying the overlap; and overlap is not linearizability, because concurrent writes and partial read repairs still produce orders no single copy would.

### CAP and PACELC placements

| System | Healthy | During a partition | PACELC | The gotcha to mention |
|---|---|---|---|---|
| ZooKeeper | linearizable writes | minority side stops serving | PC/EC | reads are served locally and can be stale unless the client calls `sync` first |
| etcd | linearizable reads and writes | minority side refuses | PC/EC | Raft; every write is a majority round trip |
| Spanner | external consistency: linearizable and serializable | refuses without a quorum | PC/EC | TrueTime commit-wait is the price of global order |
| Dynamo-style store | eventual, tunable N/W/R | keeps serving via sloppy quorum | PA/EL | hinted handoff means W acks may be on stand-ins |
| Cassandra | per operation, `ONE` to `QUORUM` to `ALL` | serves at `ONE`, refuses at `QUORUM` | PA/EL | last-writer-wins by timestamp drops the loser silently |
| MongoDB | reads from the primary, so consistent when healthy | a minority primary accepts writes until it steps down | PA/EC | those writes are rolled back; majority concerns close the gap |
| Redis | linearizable per instance, single-threaded | failover promotes a possibly stale replica | PA/EL | asynchronous replication can lose acknowledged writes |

!!! warning "Common mistake"
    Claiming "quorum reads and writes give strong consistency", or "we chose CA". W + R > N returns the newest acknowledged write and nothing more; linearizability needs consensus or a leader with synchronous replication. And partition tolerance is not optional on more than one machine — "CA" describes a system that stops when the network splits.

## Memory hooks

- **"Replication buys reads and availability; partitioning buys writes."** Every replica applies every write, so ~5k-20k writes/s is still the ceiling.
- **"Semi-sync is the default: one follower acks, the rest lag."** Every committed write lives on two nodes and no single failure blocks the primary.
- **"Eventually consistent is not a design."** Name the anomaly and the routing rule that prevents it.
- **"Read committed stops dirty reads; snapshot isolation stops phantoms; only serializable stops write skew."** Check-then-write invariants need serializable or a lock.
- **"W + R > N, with three asterisks: acknowledged only, not sloppy, not linearizable."**
- **"Failover without fencing is split brain."** Bump an epoch on every election and have downstream systems reject the old one.
- **"Cross-region synchronous replication is a 70-150 ms tax per commit."** Give each partition a home region instead.

## Related

- [Replication](../hld/fundamentals/replication.md) — topologies, failover, conflict resolution and repair
- [CAP, PACELC and consistency models](../hld/fundamentals/cap-pacelc-and-consistency-models.md) — the models and where systems sit
- [Transactions, 2PC, sagas and idempotency](../hld/fundamentals/transactions-and-distributed-transactions.md) — isolation levels, 2PC, sagas, idempotency keys
- [Consensus and coordination](../hld/fundamentals/consensus-and-coordination.md) — elections, leases and fencing tokens
- [Latency numbers and estimation tables](latency-and-estimation.md) — the source of every number above
- Gilbert and Lynch, "Brewer's Conjecture and the Feasibility of Consistent, Available, Partition-Tolerant Web Services" (SIGACT News 2002)
- Abadi, "Consistency Tradeoffs in Modern Distributed Database System Design" (IEEE Computer 2012)
- Berenson et al., "A Critique of ANSI SQL Isolation Levels" (SIGMOD 1995)
