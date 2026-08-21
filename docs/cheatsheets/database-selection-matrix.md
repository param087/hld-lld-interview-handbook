---
title: Database selection matrix
description: One row per store — data model, consistency, scaling pattern, query power, what it is for, what rules it out, and the sentence that justifies the choice out loud.
---
# Database selection matrix

## How to use this sheet

Find the row whose *best for* matches your top query, check *avoid when* against your second, then say the sentence. Every number comes from the [latency sheet](latency-and-estimation.md); quote one out loud to rule out the simpler option. The number is what turns a guess into a choice.

## Tables

### Data model, consistency, scale

| Store | Data model | Consistency you get | Scaling pattern | Query power |
|---|---|---|---|---|
| PostgreSQL / MySQL | relational rows, schema on write | strong on the primary; read committed by default, serializable available; replicas lag | vertical, then read replicas, then manual sharding (Vitess, Citus) | full SQL: joins, aggregates, constraints, many indexes |
| DynamoDB | items under a partition key plus optional sort key | per item; eventual by default, strong on request; secondary indexes always lag | managed, linear by partition; 1k WCU and 3k RCU per partition | point read and sort-key range; anything else needs an index |
| Cassandra | wide-column: partition key plus clustering columns | tunable per request (ONE, QUORUM, ALL); no cross-partition transactions | linear scale-out, ~5k-10k writes/s per node | one ordered partition; compare-and-set costs a Paxos round |
| MongoDB | JSON documents | tunable read and write concerns; single document atomic | replica sets, sharded by a chosen shard key | by id and secondary index, aggregation pipeline; no cross-shard joins |
| Redis | in-memory strings, hashes, lists, sorted sets, streams | linearizable per command on one node; async replication loses the tail on failover | ~100k ops/s per instance, memory-bound; cluster shards by hash slot | O(1) and O(log n) commands by key; no ad hoc queries |
| Elasticsearch | inverted index over JSON documents | near real time after refresh; no transactions | shard by document, replicas per shard; ~5k-10k docs/s per data node | full-text, filters, facets, aggregations |
| Neo4j | nodes and edges with properties | ACID on one instance | vertical plus read replicas; a graph has no clean shard cut | variable-depth traversal, shortest path, pattern match |
| InfluxDB / TimescaleDB | series: name plus labels, then timestamped points | last write wins per point; no transactions | by series and time window; 1M points/s at ~1.4 B compressed | range scans, aggregates, downsampling |
| ClickHouse / BigQuery | columnar segments in append-only parts | eventual; no per-row updates | massively parallel scan over columns | aggregates over billions of rows; poor point lookups |
| Spanner / CockroachDB | relational rows over consensus-replicated ranges | serializable across shards; external consistency on Spanner | automatic range splits; one consensus round per write | full SQL including cross-shard transactions |
| S3 | key to immutable blob, flat namespace | read-after-write for new keys; listings lag | effectively unlimited; erasure-coded | GET and PUT by key, prefix listing; no query engine |

### Best for, avoid when, the one sentence to say

| Store | Best for | Avoid when | The one sentence to say |
|---|---|---|---|
| PostgreSQL / MySQL | systems of record: orders, ledgers, inventory, accounts | past ~10k writes/s or one box's 2-20 TB | "A primary takes ~5k-20k writes/s; we need 1.7k average and 5k peak, so one primary plus read replicas, and I revisit at 10x." |
| DynamoDB | key-addressed state at any scale with no operators: sessions, carts, timelines | the queries are not knowable up front, or reports need aggregation | "Access is by user id, so one table keyed on the user; a celebrity hits the 1k WCU partition ceiling, so we suffix-shard that key." |
| Cassandra | time-ordered rows per entity: messages, events, sensor readings | you need joins, ad hoc queries or multi-partition transactions | "23k messages/s is past one primary, so wide-column on conversation plus month, clustered by time: the hot read is one ordered partition." |
| MongoDB | aggregates read and written whole whose shape varies per record: catalogues, profiles, content | the data is genuinely relational and reporting needs joins | "The document is the unit of read and write, so the hot page is one round trip; cross-document invariants stay in the application." |
| Redis | hot derived state you can rebuild: cache, sessions, counters, rate limits, leaderboards, leases | it holds the only copy of something you cannot lose | "Redis carries derived state at ~100k ops/s; if it dies we repopulate from the primary and pay latency, not data loss." |
| Elasticsearch | free text, facets and log analytics beside a system of record | it would be the source of truth, or you need transactions | "Search is a projection off the change stream, seconds behind and rebuildable; the order table stays authoritative." |
| Neo4j | variable-depth traversal: friends of friends, fraud rings, dependency graphs | the query is two hops, which an indexed join already does | "Depth is the query, so a graph engine on one cluster; a cross-shard hop is ~500 µs, so a 4-hop walk costs 2 ms before any work." |
| InfluxDB / TimescaleDB | metrics and telemetry with retention tiers and rollups | labels carry unbounded ids, which explodes the series count | "100k hosts x 100 metrics every 10 s = 1M points/s; 16 B raw is ~1.4 TB/day against ~120 GB/day compressed, so a time-series store." |
| ClickHouse / BigQuery | dashboards and ad hoc aggregates over billions of rows | point reads and updates by primary key | "1B rows x 1 KB is 1 TB scanned in a row store, ~8 min at ~2 GB/s; columnar reads only the 8 GB column, so we feed it by change data capture." |
| Spanner / CockroachDB | relational semantics past one primary, or writes in several regions | one primary still fits, or the write path is latency-critical | "We need cross-shard serializable transactions, so we pay a consensus round per write: ~500 µs in region, ~70 ms across a US east-west quorum." |
| S3 | blobs over ~1 MB written once: media, backups, logs, lake files | many small objects, partial updates, or low-latency random reads | "Bytes go to object storage through a presigned multipart upload; the row keeps key, size and checksum, so replicas stay small." |

!!! tip "Interview tip"
    Name the query, the number and the store in one breath, in that order: "the hot read is a user's last 20 messages at ~23k writes/s, past one primary, so wide-column keyed by conversation". A product name with no access pattern behind it is a guess, and the next question exposes it.

### Thresholds that force the decision

| Signal | Number to quote | What it rules out |
|---|---|---|
| Write rate | ~5k-20k writes/s per relational primary | above it: shard, go wide-column, or pay consensus |
| Data size | 2-20 TB of disk per box | above it: sharding, tiering, blobs to object storage |
| Read rate | 50k+ indexed reads/s per primary; ~100k ops/s per Redis | above it: cache tier first, replicas second |
| Scale-out unit | ~5k-10k writes/s per Cassandra node | 100k writes/s needs ~10-20 nodes before replication |
| Partition ceiling | DynamoDB 1k WCU and 3k RCU per partition | a hot key stays capped however large the table grows |
| Index rate | ~5k-10k docs/s per Elasticsearch data node | above it: more data nodes, bulk indexing |
| Geography | ~70 ms US east-west, 150 ms transatlantic | synchronous cross-region writes on a user path |

### Dominant query to store

| Dominant query | Store | Why it wins |
|---|---|---|
| Point lookup, small and hot | Redis | memory at 100 ns |
| Point lookup, large and durable | DynamoDB, Cassandra | linear by partition key |
| "Latest N for this entity" | Cassandra, DynamoDB sort key | one ordered partition |
| Joins, constraints, multi-row transactions | PostgreSQL, MySQL | ACID on one box |
| Those, past one primary or multi-region | Spanner, CockroachDB | consensus per write |
| Free text, typos, facets | Elasticsearch | inverted index, BM25 |
| Aggregates over billions of rows | ClickHouse, BigQuery | 3 of 200 columns read |
| Traversal with variable depth | Neo4j | adjacency pointers |
| Timestamp plus labels plus value | InfluxDB, TimescaleDB | delta and XOR compression |
| Whole aggregate, shifting shape | MongoDB | the document is the unit |
| Blobs over ~1 MB | S3 | cost per GB, not query power |

!!! warning "Common mistake"
    Choosing NoSQL "because it scales" at ~1k writes/s — 10x under one relational primary — trades joins, constraints and serializable transactions for headroom you will not use for years. The reverse costs as much: a 2 MB image stored in a row inflates every backup, replica and buffer-pool page.

## Memory hooks

- **"Relational until a number says otherwise."** The line is ~5k-20k writes/s and 2-20 TB per primary. Say which side of it you are on.
- **"Model the query, then the table."** Key-value and wide-column stores have no ad hoc queries: one table per access path, written in the same batch.
- **"One system of record; the rest are projections you can rebuild."** Say it before the interviewer asks how you keep three stores consistent.
- **"Partition key is the noun in the hot query; clustering key is the order you read it in."** Bucket by time when a partition can grow without bound.
- **"Hot keys ignore table size."** DynamoDB's 1k WCU is per partition, Cassandra's ~5k-10k writes/s per node. Spread the key, do not grow the cluster.
- **"Blobs out, pointers in."** Over ~1 MB goes to object storage; the row keeps key, size and checksum.

## Related

- [Choosing a database](../hld/fundamentals/databases-sql-vs-nosql.md) — the same decision as prose, with the selection tree
- [Storage engines and indexing](../hld/fundamentals/storage-engines-and-indexing.md) — why a B-tree row and an LSM row cost differently
- [Object, file, search, time-series and graph storage](../hld/fundamentals/storage-systems-zoo.md) — the non-relational rows in depth
- [Off-the-shelf building blocks](building-blocks-quick-reference.md) — one card per technology, with limits
- [Latency numbers and estimation tables](latency-and-estimation.md) — the source of every number above
- Corbett et al., "Spanner: Google's Globally-Distributed Database" (OSDI 2012)
- Amazon DynamoDB Developer Guide, "Best practices for designing and architecting with DynamoDB"
