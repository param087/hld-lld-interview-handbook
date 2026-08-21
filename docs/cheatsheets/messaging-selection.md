---
title: Queue and stream selection
description: Kafka, RabbitMQ, SQS/SNS, Pulsar, Redis Streams and Kinesis compared by ordering, delivery semantics, retention and replay, throughput, fan-out and per-message control, with the sizing arithmetic for partitions, disk and lag.
---
# Queue and stream selection

## How to use this sheet

First decide queue or log: a queue deletes on acknowledgement, a log retains and lets independent groups replay. Then pick the row that survives your ordering and replay requirements. Sizing numbers come from the [latency sheet](latency-and-estimation.md). Say the delivery semantics out loud before the interviewer asks.

## Tables

### Model, ordering, delivery, replay

| System | Model | Ordering | Delivery semantics | Retention and replay |
|---|---|---|---|---|
| Kafka | partitioned log, consumers pull | per partition: one key, one partition, one group member | at-least-once by default; idempotent producer plus transactions give effectively-once inside Kafka | time or size retention (7 days by default) plus compaction; any group replays by resetting offsets |
| RabbitMQ | AMQP broker, pushes with a prefetch window | per queue with a single consumer; gone the moment workers compete | at-least-once with per-message acknowledgement; nack requeues | none: the message is deleted on ack, so replay means republishing |
| SQS standard / SNS | managed queue, pull with a visibility timeout | none in standard; per message group in FIFO | at-least-once in standard; effectively-once inside a FIFO deduplication window | until deleted or the retention window expires; no replay afterwards |
| Pulsar | segmented log on BookKeeper, subscriptions per topic | per partition, or per key with key-shared subscriptions | at-least-once; effectively-once with transactions | retention plus tiered storage to object storage; rewind a subscription to replay |
| Redis Streams | in-memory log with consumer groups | per stream | at-least-once through the pending-entries list and an explicit ack | only while it fits memory and the length cap; replay by reading from id 0 |
| Kinesis | sharded managed log | per shard | at-least-once; deduplicate downstream | a configurable window; replay inside it with a shard iterator |

### Throughput, fan-out, per-message control, use it when

| System | Throughput ceiling | Fan-out | Per-message ack, delay, priority | Use it when |
|---|---|---|---|---|
| Kafka | ~100 MB/s in and ~1 GB/s out per broker; consumer parallelism capped by partition count | extra consumer groups read from the page cache, so nearly free | no, no, no | several teams consume the same events, you need replay, or throughput is in hundreds of MB/s |
| RabbitMQ | the queue is the unit and one queue is one process; scale by sharding queues | exchanges and bindings copy into many queues | yes, by plugin or TTL, yes | the unit of work is a task with a lifecycle: ack, requeue, priority, routing |
| SQS standard / SNS | effectively unlimited in standard; FIFO is far slower per message group | SNS fans one publish out to many queues | visibility timeout, yes up to 15 minutes, no | you want zero operations: managed retries, dead-letter redrive and delay |
| Pulsar | brokers and storage scale separately, so add either | subscriptions: exclusive, failover, shared, key-shared | yes, native delay, no | one cluster must serve queue and stream semantics for many teams |
| Redis Streams | ~100k ops/s per instance, memory-bound | consumer groups over one stream | yes through the pending list, no, no | you already run Redis and the stream stays small and short-lived |
| Kinesis | provisioned per shard; add shards to add throughput | several applications read the same shard independently | no, no, no | you are inside AWS and want log semantics without operating brokers |

!!! tip "Interview tip"
    Introduce a broker with its contract in one breath: "order events go on a topic keyed by order id, so each order is ordered on one partition; consumers are at-least-once and idempotent, and anything failing three times goes to a dead-letter topic." Naming the key, the semantics and the failure path is what separates a candidate who has run a consumer from one who has read about queues.

### Sizing arithmetic

| Question | Rule | Worked example |
|---|---|---|
| How many partitions? | peak rate divided by per-consumer throughput, then 2-3x headroom | 70k msg/s peak at ~1k/s per consumer needs 70 members, so create ~200; adding partitions later remaps the key hash and breaks per-key order |
| How much disk? | daily volume x retention x replication factor | 100M messages/day x 1 KB = 100 GB/day; x 7 days x RF 3 = 2.1 TB, inside one server's 2-20 TB |
| Is the group healthy? | the lag trend, not the lag level | 1.2k/s of ingest against consumers doing 1k/s falls behind 200/s, which is 17M records a day; catch-up capacity must exceed arrivals |
| What does durability cost? | one extra same-datacenter round trip per batch | ~500 µs for the in-sync replicas, amortised over hundreds of records; across regions it would be ~70 ms, so mirror asynchronously instead |
| Why is a log fast? | sequential appends, page-cache reads | an HDD streams ~150 MB/s but does ~100 random IOPS, which is 400 KB/s: roughly 400x less |

### Delivery semantics, said precisely

| You claim | What you must have | What it still accepts |
|---|---|---|
| at-most-once | commit the offset or ack before processing | lost messages on any crash |
| at-least-once | durable acks, commit after processing, bounded retries | duplicates from every retry, rebalance and restart |
| effectively-once | at-least-once plus one dedup point: a unique business id, an upsert, or the partition and offset | the dedup store, its size and its TTL |
| Kafka transactions | idempotent producer, a transactional id, read-committed consumers | Kafka-to-Kafka only; a side effect elsewhere needs an idempotent sink or a transactional outbox |

### Failure handling, whichever broker you pick

| Symptom | Cause | Fix |
|---|---|---|
| One partition's lag climbs, the rest are flat | a poison message retried in place blocks everything behind it | bounded retries, then a dead-letter topic with source headers; commit and alert on its depth |
| Lag flat but never zero | consumers exactly match ingest, so no outage ever drains | add members up to the partition count, then add partitions |
| Duplicates in the database | at-least-once meeting a non-idempotent consumer | unique constraint on the business id, or upsert keyed by it |
| One tenant saturates a partition | the key hashes to one partition regardless of cluster size | dedicated partitions for that key, or salt it and give up its ordering |
| Rebalances loop | processing exceeds the poll interval, so members are evicted | smaller batches, longer interval, sticky or cooperative rebalancing |

!!! warning "Common mistake"
    Promising ordering at the topic level, or exactly-once with no dedup point. Consumers see partitions interleaved, and every retry, rebalance and restart replays records. Say "ordering is per partition" and "the consumer dedupes on the event id" before you are asked; claiming more loses more credit than claiming nothing.

## Memory hooks

- **"Queue deletes, log retains."** Delete-on-ack gives per-message control; retention gives replay and free fan-out. Everything else follows from that fork.
- **"Ordering is per partition, never per topic."** The partition key is your ordering domain, so pick the entity that actually needs order.
- **"Effectively-once equals at-least-once plus an idempotent consumer."** Name the dedup key in the same sentence.
- **"Partitions come from consumers, disk comes from retention."** Parallelism is capped by partition count; the cluster is sized by the retention window.
- **"Retry the transient, dead-letter the deterministic."** A bad schema will never succeed, so it must never block its partition.
- **"Alert on lag in seconds behind the tail."** When lag in time approaches the retention window you are hours from silent data loss.

## Related

- [Messaging, queues and Kafka internals](../hld/fundamentals/messaging-and-event-streaming.md) — partitions, ISR, offsets and semantics explained
- [Design a distributed message queue](../hld/case-studies/distributed-message-queue.md) — the broker built end to end
- [Transactions, 2PC, sagas and idempotency](../hld/fundamentals/transactions-and-distributed-transactions.md) — idempotency keys and the outbox
- [Off-the-shelf building blocks](building-blocks-quick-reference.md) — one card per technology, with limits
- [Latency numbers and estimation tables](latency-and-estimation.md) — the source of every number above
- Kreps, Narkhede and Rao, "Kafka: a Distributed Messaging System for Log Processing" (NetDB 2011)
- KIP-98, "Exactly Once Delivery and Transactional Messaging"
