---
title: HLD round checklist
description: The system design round as a tick list — what must be on the board at each minute, the sentence that proves it, the red flags interviewers score against, and ten deep-dive openers that work on any prompt.
---
# HLD round checklist

## How to use this sheet

Run it twice: once the night before, to see which step you skip under pressure, and once as a silent checklist during a mock. The clock is the same one in [The 45-minute HLD framework](../hld/fundamentals/interview-framework.md); this page adds the artifact, the phrase and the thing to cut when you are late.

## Tables

### The clock, with what must exist before you move on

| Minutes | Step | On the board before you move on | Say it out loud | Cut this first if late |
|---|---|---|---|---|
| 0-5 | Requirements | 3-5 functional verbs, the non-functional numbers, an out-of-scope list | "I will design for X and Y; Z is out of scope." | Edge cases, admin features, analytics |
| 5-9 | Estimation | Write QPS, read QPS, storage per year, and one of bandwidth or cache size | "Reads are 100x writes, so this design is read-optimised." | Bandwidth if payloads are small |
| 9-14 | API and data model | 3-5 endpoints, entities with primary and partition keys, one store per entity | "Partitioned by user_id because the hot query is per user." | Column lists, response schemas |
| 14-24 | High-level design | The v1 diagram, plus a narrated write path and read path | "Let me trace one write, then one read." | Secondary flows; keep the two paths |
| 24-40 | Deep dives | 2-3 cruxes, each with options, a number and a pick | "The hard part is X; here are two ways and why I take the second." | The third crux, not the depth of the first |
| 40-45 | Wrap-up | Bottleneck at 10x, single points of failure, the trade-off table | "If I had another week I would change..." | The recap, never the trade-off table |

### The four numbers and the decision each one buys

| Number | Arithmetic | What it decides |
|---|---|---|
| Write QPS | daily writes / 10^5 s, then x3 for peak | one relational primary handles 5k-20k writes/s; above that you shard |
| Read QPS | write QPS x read/write ratio | at ~1k QPS per stateless app server, 500k reads/s is 500 nodes before headroom, so cache first |
| Storage per year | writes/day x object size x 365, x3 for replication | a server holds 2-20 TB, so anything larger is sharded from day one |
| Cache or bandwidth | 20% of daily reads x object size; or QPS x payload | cache tier size, or whether media belongs behind a CDN |

### Artifact tick list

| Step | Tick when the board shows it |
|---|---|
| Requirements | functional verbs numbered, and ticked off again after the v1 |
| Requirements | availability written as downtime, not as nines |
| Requirements | out-of-scope list, spoken not implied |
| Estimation | every figure with its arithmetic beside it |
| Estimation | one sentence per number naming the design decision it forces |
| API | resource nouns with HTTP verbs, versioned path |
| API | an idempotency key on every write a client may retry |
| API | an opaque cursor, never a page number, on every list |
| Data model | partition key for anything that outgrows one machine |
| Diagram | data flowing left to right, busiest arrows labelled with QPS |
| Diagram | one path per functional requirement, checked against the list |
| Deep dive | for each crux: options, the separating number, the pick, the failure mode |

### Red flags, and what the interviewer writes down

| Red flag | What it reads as | Fix in the moment |
|---|---|---|
| First box drawn before the first number | guessing at scale | stop, do the four numbers, then resume |
| Ten functional requirements, nothing out of scope | cannot prioritise | name three, park the rest aloud |
| Naming a technology with no property | resume-driven design | say the property you need, then the product that has it |
| A queue with no consumer story | decoration | name the consumer, its lag budget and its retry policy |
| "It's eventually consistent" with no window | hand-waving | give the staleness in seconds and who tolerates it |
| Microservices split before the monolith hurts | complexity for its own sake | one service per hard boundary, not per noun |
| Defending a choice with adjectives after a counter-example | fragility | restate the concern, quantify, offer two options, pick |
| Silence longer than ten seconds | cannot think aloud | narrate the option you are weighing, even unfinished |
| A diagram with no failure story | never operated a system | name the box that dies and what the client sees |
| Minute 30 with no deep dive started | pacing failure | announce the crux and start it now, mid-sentence |

### Ten deep-dive openers that fit any prompt

| # | Opener | Where it goes |
|---|---|---|
| 1 | "What happens when one key takes ten percent of the traffic?" | hot key, hot partition, split keys, local cache |
| 2 | "One write has to reach N readers: on write or on read?" | fan-out on write versus fan-out on read, hybrid for outliers |
| 3 | "The client retries and does not know whether the first call landed." | idempotency key, dedup window, effectively-once |
| 4 | "The cache is cold or a hot entry expires." | thundering herd, request coalescing, staggered TTLs, warm-up |
| 5 | "Which operations need strong consistency and which can lag?" | consistency boundary, quorum, read-your-writes |
| 6 | "What is ordered, and per what key?" | per-partition sequencing, logical clocks, gap detection |
| 7 | "The downstream dependency is slow, not down." | timeouts, circuit breaker, bulkhead, load shedding, backpressure |
| 8 | "The leader for this shard dies mid-write." | leader election, failover window, replication lag, data loss budget |
| 9 | "This data is five years old, and a user asks to delete it." | retention tiers, archival to object storage, deletion propagation |
| 10 | "How does v1 become v2 with no downtime?" | dual writes, backfill, shadow reads, cutover, rollback |

### The last five minutes

| Question | Shape of the answer |
|---|---|
| What breaks first at 10x? | Name one component, the number that saturates it, the next fix (not a redesign). |
| Where is the single point of failure? | Point at a box, say what the client sees when it dies, say the redundancy you would add. |
| Where is consistency weakest? | Name the window in seconds and who is harmed by it. |
| What would you change with another week? | One thing, with the cost of not doing it. |

## Memory hooks

- **"No boxes before the four numbers."** Write QPS, read QPS, storage per year, cache or bandwidth.
- **"Day is 10^5 seconds, peak is 3x."** Everything in step 2 falls out of those two.
- **Clock checkpoints: 5, 9, 14, 24.** If you are past one, cut something rather than compressing the deep dives.
- **"Three nines is 8.76 hours a year; four nines is 52.6 minutes."** Four nines rules out manual failover.
- **Every hop costs a same-datacenter round trip, about 500 µs.** Gateway, service, cache, database is roughly 2 ms of pure network.
- **Deep dive shape: options, number, pick, failure.** A deep dive without a number is an opinion.

!!! tip "Interview tip"
    Announce the cruxes the moment the v1 is drawn: "the hard parts are fan-out and cache sizing; I will take fan-out first." It converts the interviewer's "but what about..." from a hole in your design into agreement with your plan, and it buys you the right to skip the parts you called boring.

!!! warning "Common mistake"
    Treating the wrap-up as optional. Candidates who run the clock to minute 45 on a deep dive lose the whole bottleneck-and-trade-off row, which is cheap to score and heavily weighted. Set a hard stop at minute 40 even mid-argument, and spend the last five minutes on what breaks, what is single-homed and what you traded.

## Related

- [The 45-minute HLD framework](../hld/fundamentals/interview-framework.md) — the method behind this list, with the canonical pacing table
- [Back-of-envelope estimation](../hld/fundamentals/estimation.md) — how to produce the four numbers in four minutes
- [Clarifying-question bank](questions-to-ask.md) — the questions for minutes 0 to 5, grouped by topic
- [Latency numbers and estimation tables](latency-and-estimation.md) — every figure quoted above
- [Common SDE2 mistakes in design rounds](common-mistakes-sde2.md) — the red-flag table in depth
- [API design for HLD rounds](../hld/fundamentals/api-design.md) — cursors, idempotency keys and versioning for step 3
