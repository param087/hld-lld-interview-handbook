---
title: Glossary
description: Every term this handbook uses, defined in one line and linked to the page that teaches it — the vocabulary an SDE2 is expected to use precisely in an HLD or LLD round.
---
# Glossary

## How to use this sheet

Scan a group, then open the linked page for the terms you could not define aloud. Use these spellings in the room: the wrong word costs credibility the design then has to win back.

## Tables

### Estimation and performance

| Term | One line |
|---|---|
| [QPS](latency-and-estimation.md) | Requests per second at a boundary |
| [Peak QPS](latency-and-estimation.md) | Three times average, unless told otherwise |
| [Back-of-envelope estimation](../hld/fundamentals/estimation.md) | Order-of-magnitude capacity math, done aloud |
| [p50 / p95 / p99](../hld/fundamentals/observability-and-slos.md) | Latency percentiles; p99 is the tail |
| [DAU / MAU](../hld/fundamentals/estimation.md) | Daily and monthly active users |
| [Working set](../hld/fundamentals/caching-and-cdn.md) | The hot fraction worth caching |
| [Nines](latency-and-estimation.md) | Availability restated as yearly downtime |

### Networking, APIs and the edge

| Term | One line |
|---|---|
| [DNS](../hld/fundamentals/networking-essentials.md) | Name to address, cached by TTL |
| [WebSocket](../hld/fundamentals/networking-essentials.md) | One long-lived connection, both sides push |
| [SSE](../hld/fundamentals/networking-essentials.md) | Server-to-client stream over plain HTTP |
| [Long polling](../hld/fundamentals/networking-essentials.md) | Request held open awaiting an event |
| [REST](../hld/fundamentals/api-design.md) | Resource nouns, HTTP verbs as actions |
| [gRPC](../hld/fundamentals/api-design.md) | Binary RPC over HTTP/2, schema first |
| [GraphQL](../hld/fundamentals/api-design.md) | One endpoint, client picks the shape |
| [Idempotency key](../hld/fundamentals/api-design.md) | Client-supplied dedup key on a write |
| [Cursor pagination](../hld/fundamentals/api-design.md) | Opaque position token, not a page number |
| [Webhook](../hld/fundamentals/api-design.md) | The provider calls you back, signed |
| [API gateway](../hld/fundamentals/load-balancing-and-api-gateway.md) | Edge policy: auth, routing, quotas |
| [Layer 4 load balancer](../hld/fundamentals/load-balancing-and-api-gateway.md) | Routes connections by address and port |
| [Layer 7 load balancer](../hld/fundamentals/load-balancing-and-api-gateway.md) | Routes requests by path or header |

### Caching and CDNs

| Term | One line |
|---|---|
| [Cache-aside](../hld/fundamentals/caching-and-cdn.md) | Caller checks, then populates the cache |
| [Read-through](../hld/fundamentals/caching-and-cdn.md) | The cache loads on a miss |
| [Write-through](../hld/fundamentals/caching-and-cdn.md) | Cache and store written together |
| [Write-back](../hld/fundamentals/caching-and-cdn.md) | Cache now, store flushed later |
| [Write-around](../hld/fundamentals/caching-and-cdn.md) | Store now, cache fills on read |
| [Cache stampede](../hld/fundamentals/caching-and-cdn.md) | Many callers rebuilding one expired entry |
| [TTL](../hld/fundamentals/caching-and-cdn.md) | Time to live before expiry |
| [CDN](../hld/fundamentals/caching-and-cdn.md) | Edge network serving bytes near users |
| [Eviction policy](../lld/problems/in-memory-cache.md) | How a full cache picks victims |

### Databases and storage engines

| Term | One line |
|---|---|
| [OLTP](../hld/fundamentals/databases-sql-vs-nosql.md) | Many small indexed transactional operations |
| [OLAP](../hld/fundamentals/databases-sql-vs-nosql.md) | Few large scans over columns |
| [Denormalisation](../hld/fundamentals/databases-sql-vs-nosql.md) | Duplicating data so reads avoid joins |
| [Wide-column store](../hld/fundamentals/databases-sql-vs-nosql.md) | Partition key groups, clustering key sorts |
| [B-tree](../hld/fundamentals/storage-engines-and-indexing.md) | Balanced pages updated in place |
| [LSM tree](../hld/fundamentals/storage-engines-and-indexing.md) | Memtable plus immutable files, merged later |
| [SSTable](../hld/fundamentals/storage-engines-and-indexing.md) | Immutable sorted file of key-value pairs |
| [WAL (write-ahead log)](../hld/fundamentals/storage-engines-and-indexing.md) | Durability log written before the change |
| [Compaction](../hld/fundamentals/storage-engines-and-indexing.md) | Merging files, reclaiming space and tombstones |

### Replication, quorums and partitioning

| Term | One line |
|---|---|
| [Leader / follower](../hld/fundamentals/replication.md) | One writable replica, several read-only copies |
| [Read replica](../hld/fundamentals/replication.md) | A follower that offloads reads |
| [Replication lag](../hld/fundamentals/replication.md) | How far a follower trails |
| [Synchronous replication](../hld/fundamentals/replication.md) | Acknowledge only after a replica confirms |
| [Leaderless replication](../hld/fundamentals/replication.md) | Any replica takes writes, quorums decide |
| [Failover](../hld/fundamentals/replication.md) | Promoting a follower after leader death |
| [Split brain](../hld/fundamentals/replication.md) | Two nodes each believing they lead |
| [Quorum (N, W, R)](../hld/fundamentals/replication.md) | Replica counts where W plus R exceeds N |
| [Last-write-wins](../hld/fundamentals/replication.md) | Resolve by timestamp; silently loses writes |
| [Sharding](../hld/fundamentals/partitioning-and-consistent-hashing.md) | One dataset split across machines |
| [Partition key](../hld/fundamentals/partitioning-and-consistent-hashing.md) | The field choosing a row's shard |
| [Consistent hashing](../hld/fundamentals/partitioning-and-consistent-hashing.md) | Ring placement so few keys move |
| [Virtual nodes](../hld/fundamentals/partitioning-and-consistent-hashing.md) | Many ring positions per machine |
| [Hot partition](../hld/fundamentals/partitioning-and-consistent-hashing.md) | One shard taking most traffic |
| [Hot key](../hld/fundamentals/partitioning-and-consistent-hashing.md) | One key taking most traffic |

### Transactions and isolation

| Term | One line |
|---|---|
| [ACID](../hld/fundamentals/transactions-and-distributed-transactions.md) | Atomic, consistent, isolated, durable |
| [read committed](../hld/fundamentals/transactions-and-distributed-transactions.md) | No dirty reads; the usual default |
| [repeatable read](../hld/fundamentals/transactions-and-distributed-transactions.md) | A row reads the same twice |
| [serializable](../hld/fundamentals/transactions-and-distributed-transactions.md) | As if transactions ran sequentially |
| [snapshot isolation](../hld/fundamentals/transactions-and-distributed-transactions.md) | Each transaction reads one consistent version |
| [Write skew](../hld/fundamentals/transactions-and-distributed-transactions.md) | Concurrent writes break a cross-row rule |
| [2PC](../hld/fundamentals/transactions-and-distributed-transactions.md) | Prepare then commit; blocks on coordinator loss |
| [Saga](../hld/fundamentals/transactions-and-distributed-transactions.md) | Local transactions plus compensations |
| [Transactional outbox](../hld/fundamentals/transactions-and-distributed-transactions.md) | Event written in the same transaction |

### Consistency models

| Term | One line |
|---|---|
| [CAP theorem](../hld/fundamentals/cap-pacelc-and-consistency-models.md) | In a partition, consistency or availability |
| [PACELC](../hld/fundamentals/cap-pacelc-and-consistency-models.md) | And when healthy, latency or consistency |
| [Strong consistency](../hld/fundamentals/cap-pacelc-and-consistency-models.md) | Reads see the latest committed write |
| [Eventual consistency](../hld/fundamentals/cap-pacelc-and-consistency-models.md) | Replicas converge once writes stop |
| [Causal consistency](../hld/fundamentals/cap-pacelc-and-consistency-models.md) | Related writes are seen in order |
| [Linearizable](../hld/fundamentals/cap-pacelc-and-consistency-models.md) | One global order respecting real time |
| [Read-your-writes](../hld/fundamentals/cap-pacelc-and-consistency-models.md) | You see your own last write |
| [Monotonic reads](../hld/fundamentals/cap-pacelc-and-consistency-models.md) | Reads never go backwards |
| [CRDT](../hld/case-studies/collaborative-editor.md) | A type whose concurrent merges converge |

### Messaging, streaming and feeds

| Term | One line |
|---|---|
| [Message queue](../hld/fundamentals/messaging-and-event-streaming.md) | Work items consumed once, then removed |
| [Publish/subscribe](../hld/fundamentals/messaging-and-event-streaming.md) | One event to every interested subscriber |
| [Topic and partition](../hld/fundamentals/messaging-and-event-streaming.md) | The unit of ordering and parallelism |
| [Consumer group](../hld/fundamentals/messaging-and-event-streaming.md) | Consumers sharing one topic's partitions |
| [Consumer offset](../hld/fundamentals/messaging-and-event-streaming.md) | The position a consumer has committed |
| [At-most-once](../hld/fundamentals/messaging-and-event-streaming.md) | May be lost, never duplicated |
| [At-least-once](../hld/fundamentals/messaging-and-event-streaming.md) | Never lost, may be duplicated |
| [Exactly-once (effectively-once)](../hld/fundamentals/messaging-and-event-streaming.md) | At-least-once plus an idempotent consumer |
| [Dead-letter queue](../hld/fundamentals/messaging-and-event-streaming.md) | Where repeatedly failing messages land |
| [Fan-out on write](../hld/case-studies/news-feed.md) | Push a post into follower feeds |
| [Fan-out on read](../hld/case-studies/news-feed.md) | Merge followed authors at read time |
| [MapReduce](../hld/fundamentals/batch-and-stream-processing.md) | Map, shuffle, reduce over partitions |

### Consensus, coordination and time

| Term | One line |
|---|---|
| [Consensus](../hld/fundamentals/consensus-and-coordination.md) | Agreement on one value despite failures |
| [Raft](../hld/fundamentals/consensus-and-coordination.md) | Consensus as a replicated log |
| [Leader election](../hld/fundamentals/consensus-and-coordination.md) | Choosing the node allowed to coordinate |
| [Lease](../hld/fundamentals/consensus-and-coordination.md) | A lock with an expiry |
| [Fencing token](../hld/fundamentals/consensus-and-coordination.md) | A rising number stopping a stale holder |
| [Lamport timestamp](../hld/fundamentals/time-and-ordering.md) | A counter giving order, not causality |
| [Vector clock](../hld/fundamentals/time-and-ordering.md) | Per-node counters that detect concurrency |
| [Hybrid logical clock](../hld/fundamentals/time-and-ordering.md) | Physical time plus a counter |

### Probabilistic structures, limits and geography

| Term | One line |
|---|---|
| [Bloom filter](../hld/fundamentals/probabilistic-data-structures.md) | Maybe present, definitely absent, few bits |
| [Count-Min Sketch](../hld/fundamentals/probabilistic-data-structures.md) | Approximate frequencies for heavy hitters |
| [HyperLogLog](../hld/fundamentals/probabilistic-data-structures.md) | Approximate distinct counts in kilobytes |
| [Token bucket](../hld/fundamentals/rate-limiting.md) | Tokens refill; bursts up to the cap |
| [Leaky bucket](../hld/fundamentals/rate-limiting.md) | Drains at a fixed rate |
| [Fixed window counter](../hld/fundamentals/rate-limiting.md) | Count per window; doubles at boundaries |
| [Sliding window log](../hld/fundamentals/rate-limiting.md) | Exact and expensive: keep every timestamp |
| [Sliding window counter](../hld/fundamentals/rate-limiting.md) | Two windows blended; the production choice |
| [Geohash](../hld/fundamentals/geospatial-indexing.md) | Coordinates interleaved into a string prefix |

### Resilience, observability and delivery

| Term | One line |
|---|---|
| [Circuit breaker](../hld/fundamentals/resilience-patterns.md) | Stop calling a failing dependency |
| [Bulkhead](../hld/fundamentals/resilience-patterns.md) | Separate pools so failure stays local |
| [Backpressure](../hld/fundamentals/resilience-patterns.md) | Telling the producer to slow down |
| [Load shedding](../hld/fundamentals/resilience-patterns.md) | Reject low-value work under stress |
| [Exponential backoff](../hld/fundamentals/resilience-patterns.md) | Doubling the wait between retries |
| [Graceful degradation](../hld/fundamentals/resilience-patterns.md) | A worse answer rather than none |
| [SLI](../hld/fundamentals/observability-and-slos.md) | The measured indicator |
| [SLO](../hld/fundamentals/observability-and-slos.md) | The internal target for it |
| [SLA](../hld/fundamentals/observability-and-slos.md) | The contract, with money attached |
| [Error budget](../hld/fundamentals/observability-and-slos.md) | The failure an SLO permits |
| [Distributed tracing](../hld/fundamentals/observability-and-slos.md) | Spans stitched into one request's journey |
| [Blue-green deployment](../hld/fundamentals/deployment-and-data-migrations.md) | Two environments, traffic switched at once |
| [Canary release](../hld/fundamentals/deployment-and-data-migrations.md) | New version on a small traffic share |
| [Feature flag](../hld/fundamentals/deployment-and-data-migrations.md) | Release separated from deploy |

### Architecture, storage systems and security

| Term | One line |
|---|---|
| [Monolith](../hld/fundamentals/microservices-and-architecture-styles.md) | One deployable; the right default first |
| [Microservice](../hld/fundamentals/microservices-and-architecture-styles.md) | One service per hard boundary |
| [CQRS](../hld/fundamentals/microservices-and-architecture-styles.md) | Separate write model from read model |
| [Event sourcing](../hld/fundamentals/microservices-and-architecture-styles.md) | Store events, derive the state |
| [Horizontal scaling](../hld/fundamentals/scaling-primer.md) | More machines, not bigger ones |
| [Stateless service](../hld/fundamentals/scaling-primer.md) | No client state; any replica serves |
| [Object storage](../hld/fundamentals/storage-systems-zoo.md) | Flat keyed blobs, cheap and unbounded |
| [Inverted index](../hld/fundamentals/storage-systems-zoo.md) | Term to document list, behind search |
| [OAuth 2.0](../hld/fundamentals/security-essentials.md) | Delegated authorisation by token |
| [JWT](../hld/fundamentals/security-essentials.md) | Signed self-contained token; revocation is hard |
| [Allowlist / denylist](../hld/fundamentals/security-essentials.md) | Explicitly permitted, or explicitly refused |

### Object-oriented design

| Term | One line |
|---|---|
| [Single responsibility](../lld/fundamentals/solid-principles.md) | One reason to change per class |
| [Open/closed](../lld/fundamentals/solid-principles.md) | Extend by adding, not by editing |
| [Liskov substitution](../lld/fundamentals/solid-principles.md) | A subtype keeps the base's promises |
| [Interface segregation](../lld/fundamentals/solid-principles.md) | Many small interfaces, not one fat |
| [Dependency inversion](../lld/fundamentals/solid-principles.md) | Depend on abstractions, wire at the root |
| [DRY](../lld/fundamentals/design-principles-beyond-solid.md) | One home per reason to change |
| [KISS](../lld/fundamentals/design-principles-beyond-solid.md) | The simplest thing that works |
| [YAGNI](../lld/fundamentals/design-principles-beyond-solid.md) | Build it when a requirement asks |
| [Law of Demeter](../lld/fundamentals/design-principles-beyond-solid.md) | Talk to neighbours, not their neighbours |
| [GRASP](../lld/fundamentals/design-principles-beyond-solid.md) | Nine answers to which class owns |
| [Cohesion](../lld/fundamentals/design-principles-beyond-solid.md) | How related one class's contents are |
| [Coupling](../lld/fundamentals/design-principles-beyond-solid.md) | Knowledge one class needs of another |
| [Value object](../lld/fundamentals/oop-in-python.md) | Immutable, compared by value |
| [Entity](../lld/fundamentals/oop-in-python.md) | Mutable, identified by id, guards transitions |
| [Protocol](../lld/fundamentals/oop-in-python.md) | Structural interface: shape, not inheritance |
| [Composition over inheritance](../lld/fundamentals/oop-in-python.md) | Prefer has-a to is-a |
| [snake_case / PascalCase](../lld/fundamentals/oop-in-python.md) | Functions and variables, versus classes |

### Contracts, UML and design smells

| Term | One line |
|---|---|
| [Invariant](../lld/fundamentals/interfaces-and-contracts.md) | True before and after every call |
| [Multiplicity](../lld/fundamentals/uml-with-mermaid.md) | The number on a relation |
| [Composition (UML)](../lld/fundamentals/uml-with-mermaid.md) | The part dies with the whole |
| [Aggregation (UML)](../lld/fundamentals/uml-with-mermaid.md) | The part outlives the whole |
| [Anemic model](../lld/fundamentals/lld-interview-framework.md) | Data-only classes, logic in a service |
| [God object](../lld/fundamentals/lld-interview-framework.md) | A class whose name needs "and" |
| [Pattern-itis](../lld/patterns/patterns-overview.md) | Patterns without an axis of change |

### Concurrency and testing in LLD

| Term | One line |
|---|---|
| [GIL](../lld/fundamentals/concurrency-for-lld.md) | Protects the interpreter, not your invariants |
| [Race condition](../lld/fundamentals/concurrency-for-lld.md) | Correctness depending on thread timing |
| [Deadlock](../lld/fundamentals/concurrency-for-lld.md) | Two holders waiting on each other |
| [Reentrant lock](../lld/fundamentals/concurrency-for-lld.md) | The same thread may reacquire it |
| [Condition variable](../lld/fundamentals/concurrency-for-lld.md) | Wait for a predicate without spinning |
| [Lock ordering](../lld/fundamentals/concurrency-for-lld.md) | Multiple locks taken in one order |
| [Optimistic concurrency control](../lld/fundamentals/concurrency-for-lld.md) | Check a version on write, retry |
| [Fake](../lld/fundamentals/clean-code-and-testing.md) | A simple working double for tests |
| [Mock](../lld/fundamentals/clean-code-and-testing.md) | A double asserting on calls |
| [Fake clock](../lld/fundamentals/clean-code-and-testing.md) | Injected time, so tests never sleep |

## Memory hooks

- **Say the canonical word:** leader and follower, allowlist and denylist, p99, QPS, effectively-once.
- **Effectively-once = at-least-once + idempotent consumer.** There is no other kind.
- **CAP applies only during a partition; PACELC covers the rest of the year.**
- **Availability is a duration, not a count of nines.** Convert before you speak.
- **Fan-out on write pays at publish; fan-out on read pays at every view.**

!!! tip "Interview tip"
    Define a term in five words the moment you use it: "sloppy quorum, meaning any live node may accept the write". It proves you are not repeating a phrase, and gives the interviewer a cheap place to correct you.

!!! warning "Common mistake"
    Reaching for an approximate word: "eventually consistent" with no staleness window, "exactly-once" in a system full of retries, "linearizable" for anything strong-ish. Interviewers probe whichever term you chose, so use the loose one only when the precise one is ready.

## Related

- [Latency numbers and estimation tables](latency-and-estimation.md) — every figure quoted here
- [Design patterns overview](../lld/patterns/patterns-overview.md) — the pattern vocabulary
- [CAP, PACELC and consistency models](../hld/fundamentals/cap-pacelc-and-consistency-models.md) — consistency terms in depth
- [Clarifying-question bank](questions-to-ask.md) — the terms you need first
- [Classic papers digest](../hld/fundamentals/classic-papers-digest.md) — where these names were coined
