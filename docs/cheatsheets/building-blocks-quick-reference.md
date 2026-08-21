---
title: Off-the-shelf building blocks
description: One card per component you are allowed to draw as a box — what it is, its data model, the limit to quote, and the moment in a design when you reach for it.
---
# Off-the-shelf building blocks

## How to use this sheet

These thirteen cover almost every box on an HLD whiteboard. Draw one only when you can say its data model and its limit in the same breath — that sentence is what separates naming a technology from choosing it. Capacity figures come from the [latency sheet](latency-and-estimation.md).

## Tables

### The thirteen blocks

| Block | What it is | Data model | The limit to quote | Reach for it when |
|---|---|---|---|---|
| Redis | single-threaded in-memory data-structure server | keys to strings, hashes, lists, sets, sorted sets and streams, with atomic commands and Lua scripts | ~100k ops/s per instance, memory-bound; async replication can lose the tail on failover | cache, sessions, counters, rate limits, leaderboards, leases, small queues |
| Memcached | multithreaded string cache with a slab allocator | key to opaque bytes; no structures, no persistence, no replication | ~200k+ ops/s per node; scaled entirely by client-side consistent hashing | one large blob cache on big multicore boxes and nothing more |
| Kafka | replicated partitioned log; consumers pull and own their offsets | topics split into partitions of offset-addressed records; the key picks the partition | ~100 MB/s in and ~1 GB/s out per broker; ordering per partition only; no delay or priority | several teams consume the same events, replay matters, or throughput is hundreds of MB/s |
| RabbitMQ | AMQP broker that pushes to workers and deletes on acknowledgement | exchanges route to queues by binding key; per-message ack, TTL and priority | no replay; one queue is one process, so scale by sharding queues | the unit of work is a task with a lifecycle needing per-message control |
| Cassandra | leaderless wide-column store on an LSM engine | partition key plus clustering columns; one table per query | ~5k-10k writes/s per node; no joins, no cross-partition transactions | time-ordered rows per entity past one primary: messages, events, sensor data |
| DynamoDB | managed key-value and document store with per-partition capacity | partition key plus optional sort key; secondary indexes always lag | 1k WCU and 3k RCU per partition, so a hot key is capped whatever the table size | access is by key, every query is knowable, and you want no operators |
| PostgreSQL | relational system of record on a B+tree engine with MVCC | typed rows, constraints, joins, many secondary indexes | ~5k-20k writes/s and 50k+ indexed reads/s on one primary, 2-20 TB per box | anything transactional under ~10k writes/s: the default, until a number says otherwise |
| Elasticsearch | distributed inverted index over JSON, near real time | documents analyzed into terms; shard by document, replicas per shard | ~5k-10k docs/s per data node; no transactions; a projection, never the source of truth | full text, typo tolerance, facets, log analytics |
| ZooKeeper / etcd | small strongly consistent store for coordination, not data | znodes with ephemeral nodes and watches; etcd is a flat keyspace with leases and revisions | every write is atomic broadcast, so 3-5 nodes do thousands of writes/s, not hundreds of thousands | leader election, membership, configuration, and leases with fencing tokens |
| S3 | object storage: immutable blobs in a flat namespace behind HTTP | key to blob plus metadata; a prefix listing is a range scan, not a tree walk | no partial updates; small objects waste it; listings lag | anything over ~1 MB written once: media, backups, logs, lake files, cold log segments |
| CDN | caching reverse proxies at the network edge | URL plus `Vary` to a cached response; `Cache-Control` sets the lifetime | hit ratio collapses on per-user responses; invalidation is slow, so version the URL | static assets and shared responses; a nearby edge saves a ~150 ms cross-ocean round trip |
| Flink / Spark | distributed compute over a DAG; Flink streams, Spark batches or micro-batches | keyed streams with windows and checkpointed local state, or immutable partitioned datasets | watermarks bound correctness; the checkpoint interval is your recovery window; every shuffle crosses the network | windowed aggregation, joins, sessionisation: anything beyond one record at a time |
| Nginx / Envoy | L7 reverse proxies; Envoy also runs as a service-mesh sidecar | routes by host, path and header to upstream pools with health checks | ~10k-100k QPS per node depending on TLS and payload; deploy it redundantly or it is your ceiling | TLS termination, L7 balancing, canary weights, outlier ejection |

!!! tip "Interview tip"
    Say the limit while you draw the box: "Redis for the session store, ~100k ops/s per instance, and it is derived state so a restart costs latency, not data." Interviewers cannot tell a memorised diagram from a designed one until you attach a number and a failure mode to a component, and that single sentence does both.

### Where each block sits in the request path

| Layer | Blocks | Its one job |
|---|---|---|
| Edge | CDN | serve bytes near the user and absorb static reads |
| Entry | Nginx, Envoy, API gateway | TLS, routing, health checks, rate limiting, authentication |
| Compute | stateless app servers at ~1k QPS each | business logic, holding no session state |
| Cache | Redis, Memcached | keep the store's miss rate low, and nothing you cannot rebuild |
| Store | PostgreSQL, Cassandra, DynamoDB, S3, Elasticsearch | one system of record; the rest are projections |
| Async transport | Kafka, RabbitMQ | decouple producer from consumer and absorb bursts as lag |
| Async compute | Flink, Spark | windows, joins and rollups fed off the log |
| Coordination | ZooKeeper, etcd | leader election, membership, leases with fencing tokens |

### The pairs interviewers ask you to separate

| Pair | The difference that matters | Take the first when |
|---|---|---|
| Redis vs Memcached | data structures, scripts, persistence and replication against raw string throughput | you need atomic counters, sorted sets, or a cache that survives a restart |
| Kafka vs RabbitMQ | retention and replay against per-message ack, delay and priority | several independent consumers, replay, or very high throughput |
| Cassandra vs DynamoDB | you operate it and tune N, W and R against a managed per-partition ceiling | you want the consistency level per request and control of the node count |
| S3 vs a distributed file system | flat keys over HTTP against POSIX semantics and huge sequential files | clients address objects by key and nothing needs a file handle |
| Elasticsearch vs relational full-text | analyzers, ranking and facets against one fewer system to operate | search quality is the product rather than a feature |
| ZooKeeper vs etcd | znodes, ephemeral nodes and one-shot watches against leases and resumable watches | you want ephemeral membership; for plain elections either is fine |
| Flink vs Spark | continuous event-time processing against micro-batches and batch reuse | latency must be seconds or less and the state is large |
| API gateway vs service mesh | north-south edge duties against east-west per-call policy | the traffic comes from outside your network |

!!! warning "Common mistake"
    Drawing a block and giving it a job it cannot do: counting requests in ZooKeeper, treating the search index as the source of truth, keeping the only copy of something in Redis, or hiding session state behind sticky sessions at the proxy. Each is a component used against its data model, and the follow-up question always finds it.

## Memory hooks

- **"Cache, queue, blob store, search index."** Four boxes solve most scaling problems; add coordination only when two nodes acting at once corrupts something.
- **"Redis for structures, Memcached for bytes."** ~100k ops/s against ~200k+ ops/s, and Redis is the one that can survive a restart.
- **"Kafka retains, RabbitMQ deletes."** Replay and fan-out against per-message control.
- **"Coordinate with it, never count with it."** An ensemble does thousands of writes/s; per-request counters belong in Redis or a log.
- **"A CDN is a cache you do not operate."** It saves a ~150 ms cross-ocean round trip on every shared response.
- **"One system of record; everything else is rebuildable."** Search, analytics, cache and time-series are projections.
- **"~1k QPS per app server, ~10k-100k per proxy, ~100k per Redis, ~5k-20k writes/s per primary."** Four numbers that size most first drafts.

## Related

- [Database selection matrix](database-selection-matrix.md) — the store rows compared column by column
- [Queue and stream selection](messaging-selection.md) — brokers by ordering, delivery and replay
- [Object, file, search, time-series and graph storage](../hld/fundamentals/storage-systems-zoo.md) — S3, search, time-series and graph in depth
- [Caching and CDNs](../hld/fundamentals/caching-and-cdn.md) — cache strategies, stampedes and CDN mechanics
- [Load balancing, reverse proxies and API gateways](../hld/fundamentals/load-balancing-and-api-gateway.md) — what the entry tier owns
- [Latency numbers and estimation tables](latency-and-estimation.md) — the source of every number above
