# Glossary and terminology rules

Use these terms consistently across every page. Where the industry uses several names, the **canonical** one is listed first.

## Canonical terms

| Term | Use it for | Notes |
|---|---|---|
| leader / follower | replication roles | never "master/slave" |
| allowlist / denylist | access lists | never "whitelist/blacklist" |
| QPS | requests per second at a service boundary | "TPS" only when transactions are literally meant |
| p50 / p95 / p99 | latency percentiles | write "p99 latency of 200 ms", never "99th percentile latency" in tables |
| DAU / MAU | daily / monthly active users | |
| strong consistency / eventual consistency / causal consistency | consistency models | "linearizable" when you mean the formal property |
| at-most-once / at-least-once / exactly-once (effectively-once) | delivery semantics | say "effectively-once = at-least-once + idempotent consumer" |
| idempotency key | client-supplied dedup key on a write | |
| cache-aside / read-through / write-through / write-back / write-around | caching strategies | |
| fan-out on write / fan-out on read | feed distribution | |
| hot partition / hot key | skewed load | |
| read replica | follower used for reads | |
| leader election | choosing a coordinator | |
| saga / 2PC | distributed transaction styles | |
| WAL (write-ahead log) | durability log | |
| LSM tree / B-tree | storage engine families | |
| read uncommitted / read committed / repeatable read / serializable (+ snapshot isolation) | isolation levels | SQL-standard spelling |
| consistent hashing / virtual nodes | partitioning | |
| quorum (N, W, R) | replica voting | |
| circuit breaker / bulkhead / backpressure / load shedding | resilience patterns | |
| back-of-envelope estimation | capacity math | |
| SLI / SLO / SLA / error budget | reliability targets | |
| SSE / WebSocket / long polling | realtime transports | |
| snake_case / PascalCase | Python naming | functions/variables vs classes |

## Style rules for numbers
- Always show the arithmetic: `10M DAU x 5 reads/day = 50M reads/day ~ 580 QPS; peak x3 ~ 1.7k QPS`.
- Use the ranges from `docs/cheatsheets/latency-and-estimation.md`; no false precision ("~0.5 ms", not "0.4792 ms").
- Units: `ms`, `µs` spelled as `us` in code blocks, `KB/MB/GB/TB/PB` (powers of 2 are fine for memory, powers of 10 for network).

## Banned terms
The linter fails a page containing any of these (case-insensitive, whole word or phrase).

| banned | use instead |
|---|---|
| master/slave | leader/follower |
| master-slave | leader-follower |
| slave | follower |
| whitelist | allowlist |
| blacklist | denylist |
| as an AI | (never) |
| lorem ipsum | (never) |
