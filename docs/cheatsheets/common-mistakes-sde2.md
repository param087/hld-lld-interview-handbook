---
title: Common SDE2 mistakes in design rounds
description: Forty failure modes from real HLD and LLD rounds — the symptom the interviewer sees, what it costs on the rubric, and the sentence or move that fixes it.
---
# Common SDE2 mistakes in design rounds

## How to use this sheet

Read it the morning of a round, then again while reviewing a practice recording. Each row is a symptom an interviewer can observe, the score it costs, and a fix you can say or do. These lose points quietly, which is why candidates repeat them.

## Tables

### HLD: framing, estimation and the board

| Mistake | Symptom | Why it costs | Fix |
|---|---|---|---|
| Designing before the numbers | first box at minute 2, first number at minute 25 | the cache size and shard count were guesses, so the estimation and design rows both go | requirements, then estimation, then the first box |
| Boiling the ocean in requirements | ten functional requirements, nothing out of scope | a v1 you cannot finish in the time left | three to five verbs; park the rest by name |
| Skipping estimation as "obvious" | shards and multi-region for 10M requests/day | 10M / 10^5 = ~120 QPS, one server; overbuilding reads as a memorised picture | do the division out loud before you draw |
| Numbers with no consequence | a precise QPS table that changes no decision | nobody can tell whether you understood it | end every number with "so we need..." |
| Forgetting peak | sizing for the average, meeting 3x at minute 35 | the primary breaks with no time left to fix it | say both together: "1.7k average, 5k peak" |
| A diagram with no traced path | twelve boxes and arrows everywhere | no visible write or read, so the design row has nothing to grade | trace one write and one read before adding a box |
| Deep dives without a decision | listing fan-out on write and fan-out on read, then stopping | the rubric grades the pick, not the menu | options, the number, the pick, the failure mode |
| Arguing with pushback | defending with adjectives after a counter-example | reads as inflexible and unquantified | restate, quantify, offer two options, pick, move on |
| Unfinished v1 at minute 45 | a clever half-design | steps you never reached score zero | finish a plain complete design, then go deep |
| Forgetting the bytes | sizing QPS only, when 1k photo loads/s is ~1 GB/s | NICs and egress, not CPU, are the ceiling | media to object storage and a CDN in the first diagram |

### HLD: data, scale and failure

| Mistake | Symptom | Why it costs | Fix |
|---|---|---|---|
| "NoSQL because it scales" | a wide-column store chosen at ~1k writes/s | 10x under one primary, and you gave up joins, constraints and transactions | default relational under ~10k writes/s, and say the number |
| Naming the product before the queries | "we'll use MongoDB" with no access patterns | every follow-up exposes a choice you cannot defend | top three queries with rates first, product last |
| Joining across shards on the hot path | a fan-out read over 500 shards | 500 x 500 µs = 250 ms serially, the slowest shard's p99 in parallel | co-locate rows that join, or denormalize a read model |
| Replicating to scale writes | "add replicas" as the answer to write load | every replica applies every write, so ~5k-20k writes/s is unchanged | partition for writes, replicate for reads |
| "Eventually consistent" with no lag rule | a read replica drawn with no routing rule | the user's own comment vanishes on refresh | name read-your-writes and monotonic reads, and route each |
| Sticky sessions instead of a stateless tier | user state in the app server's memory | a dead server logs its users out and autoscaling does nothing | sessions in a store or a signed token |
| Sizing the database for the cached load | a cache in front and no cold-start math | a flushed cache sends ~175k reads/s at a primary doing 50k+ | headroom for a cold cache, plus single-flight on misses |
| Retrying at every layer | browser, gateway, service and client library all retry | three attempts at three layers is 27 requests, bursting when the dependency is weakest | retry at one layer, with a budget, backoff and jitter |
| Claiming exactly-once | "the broker handles it" | retries, rebalances and restarts all replay records | at-least-once plus an idempotent consumer; name the dedup key |
| A single load balancer box | one box labelled LB in front of everything | the system inherits one machine's availability and bandwidth | draw a redundant pair or an ECMP tier, and say how failover works |

!!! tip "Interview tip"
    The cheapest points in either round come from narrating the decision, not the artifact: "two options, here is the number, here is the pick, here is what breaks". Say that four times in 45 minutes and you clear the bar even with a component wrong: the rubric grades visible reasoning.

### LLD: modelling and structure

| Mistake | Symptom | Why it costs | Fix |
|---|---|---|---|
| Pattern-itis | Factory, Builder and Observer on a problem with one flow | ceremony reads as memorisation, not judgement | every pattern arrives with the second implementation it exists for |
| The god object | `ParkingLotManager` holding spots, pricing, payments and the display | nothing can be extended or tested in isolation | split as soon as the class name needs an "and" |
| The anemic model | dataclasses of fields plus a service reaching through them | the service writes `order.lines[i].unit_price`; the domain has no behaviour | put the invariant next to the data |
| Inheriting for reuse | subclassing `list` or `dict` to add one rule | every inherited method is a way around the rule | wrap and delegate |
| Injecting the concrete type | `gateway: CardGateway` as a constructor parameter | the coupling moved, it did not go | type the parameter as the Protocol |
| A Protocol with ten methods | one fat interface every collaborator depends on | two clients use disjoint halves and both change together | segregate by client need: two Protocols |
| Reciting acronyms | "this follows SRP and DRY and GRASP" | interviewers hear memorisation, not a decision | name the decision and its consequence |
| Designing the database | tables, indexes and sharding in an LLD round | the modelling budget goes to the wrong layer | "persistence sits behind a repository interface", then back to behaviour |
| A diagram that contradicts the code | the diagram says `PricingStrategy`, the code says `PriceCalc` | the reviewer now trusts neither | rename one, immediately |
| Clarifying, then ignoring it | four minutes of questions, then a design that ignores the answers | three vehicle types when the interviewer said one | write the answers on the board and point at them |

### LLD: contracts, code and concurrency

| Mistake | Symptom | Why it costs | Fix |
|---|---|---|---|
| No working code by minute 30 | classes and diagrams, nothing runnable | the commonest SDE2 failure and the hardest to recover from | abandon a class and finish the core flow |
| Ignoring the concurrency hint | "what if two gates take the same spot?" heard as curiosity | that is the concurrency section arriving early | name the lock, what it protects, the ordering rule |
| `None` for "not found" | every caller writes an `if` | one forgets, and the failure surfaces three layers away | raise `NotFoundError` |
| Exceptions for expected outcomes | `OutOfStockError` on a normal stock check | every caller wraps in `try` and loses the count it needed | return a result object; out of stock is an answer |
| `list[T]` from a repository | a method returning every row | fine at ten rows, fatal at a million, and the fix breaks every caller | return a page from the first version |
| Boolean parameters | `charge(amount, True)` | unreadable at the call site, and it steers a branch in the callee | a keyword-only argument, an enum, or two methods |
| `__eq__` without `__hash__` | value objects that work until the first `set()` | Python sets `__hash__` to `None`, so it raises `TypeError` later | a frozen dataclass, or define both together |
| `datetime.now()` inside a service | time read from inside the domain | the only way to test it is patching, which binds tests to import paths | inject a `Clock`; the same for id generation |
| "The GIL makes it thread-safe" | `count += 1` and check-then-act with no lock | every read-modify-write is still a race | a lock per aggregate, and `while` (never `if`) around a `wait()` |
| `sleep` in a concurrency test | `time.sleep(0.1)` then an assertion | passes locally, fails on loaded CI, never forces the interleaving | a `Barrier`, a `ThreadPoolExecutor`, a bounded `result(timeout=...)` |

!!! warning "Common mistake"
    The most expensive habit in both rounds is silence while you think. An interviewer cannot grade what you did not say, so quietly reaching a good answer scores below narrating a mediocre one and correcting it out loud.

## Memory hooks

- **"Requirements, numbers, API, data, diagram, deep dives."** A box drawn before the numbers is a guess you must defend.
- **"Say the number, then say 'so'."** A figure that changes no decision earns nothing.
- **"One write path, one read path, then embellish."**
- **"Partition for writes, replicate for reads, cache the hot 20%."** Three tools, three problems.
- **"`hash(key) mod N` fails the follow-up."** One extra node in four moves ~80% of keys: say consistent hashing with virtual nodes.
- **"Every pattern needs its second implementation."** If you cannot name one, write a method.
- **"Working code beats a class diagram."** Nothing runnable at minute 30 means cut scope now.

## Related

- [The 45-minute HLD framework](../hld/fundamentals/interview-framework.md) — the pacing these break
- [The LLD interview framework](../lld/fundamentals/lld-interview-framework.md) — what interviewers grade
- [HLD round checklist](hld-checklist.md) — must-say phrases per step
- [LLD round checklist](lld-checklist.md) — the code-quality pass before you stop
- [Concurrency for LLD in Python](../lld/fundamentals/concurrency-for-lld.md) — locks and deterministic tests
- [Latency numbers and estimation tables](latency-and-estimation.md) — every number above
