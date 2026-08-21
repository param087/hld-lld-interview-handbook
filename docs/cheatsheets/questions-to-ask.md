---
title: Clarifying-question bank
description: The questions worth spending your first five minutes on, grouped by topic, each with the design decision its answer changes — plus the defaults to state aloud when the interviewer shrugs.
---
# Clarifying-question bank

## How to use this sheet

Ask only what changes the design; assume the rest out loud. Pick two or three per group, not the whole list — the ask column is the question, the second column is the reason it earns airtime. When you get no answer, take the default from the last table, say it, and move.

## Tables

### Users, actors and use cases

| Ask | What the answer changes |
|---|---|
| Who are the actors, and which one is the paying customer? | Which flow you optimise, and whose latency target binds |
| What are the top three use cases, and what is explicitly out of scope? | The functional list you tick off against the v1 diagram |
| Is this a new build or a component inside an existing product? | Whether you may assume auth, identity and a user store already exist |
| Mobile, web or both? Offline support? | Client-side caching, sync, conflict resolution, push versus poll |
| Anything different about admins, moderators or internal tools? | A second, low-QPS path that must not share the hot store |

### Scale and the read/write ratio

| Ask | What the answer changes |
|---|---|
| DAU or MAU, and actions per user per day? | Every number downstream; write QPS is daily writes / 10^5 s |
| Read-to-write ratio? | Cache-first versus write-path-first; 100:1 makes reads the whole design |
| Peak versus average, and is there a scheduled spike? | Peak is 3x average unless told otherwise; an on-sale or a live event is 10x |
| How large is one object, and how many are retained, for how long? | Storage per year, and whether one machine's 2-20 TB is enough |
| Is the load skewed — a celebrity, a flash sale, one hot tenant? | Hot key or hot partition handling, and whether fan-out on write survives |
| Growth over the next two years? | Whether you shard now or design a migration path |

### Consistency and durability

| Ask | What the answer changes |
|---|---|
| Which operation must be strongly consistent, and which may lag? | Where quorums or a single primary go, and where replicas are enough |
| How stale may a read be — a second, a minute, an hour? | Cache TTLs, replication lag budget, read-your-writes handling |
| Is a lost write ever acceptable? | Acknowledge-then-queue versus write-then-acknowledge, and the WAL story |
| Can the client retry a write it is not sure landed? | Whether every write takes an idempotency key and a dedup window |
| Does anything need ordering, and per what key? | Per-partition sequencing, single-writer partitions, gap detection |
| Can this system ever oversell, double-charge or double-deliver? | Whether you need a reservation with expiry, a saga, or a ledger |

### Latency, availability and geography

| Ask | What the answer changes |
|---|---|
| p99 target per operation, measured where — client or server? | Whether a synchronous call fits, or the work goes to a queue |
| What availability, expressed as downtime? | 99.9% is 8.76 hours a year; 99.99% is 52.6 minutes and rules out manual failover |
| What may be degraded rather than unavailable? | The load-shedding order, and which features are the first to go |
| One region or several? Where are the users? | A cross-region round trip is ~70 ms US coast to coast, ~150 ms transatlantic |
| Must data stay in a jurisdiction? | Regional partitioning by user home region, and whether a global index is even legal |
| Active-active or active-passive across regions? | Conflict resolution, failover time, and whether writes are region-local |

### Cost, security and privacy

| Ask | What the answer changes |
|---|---|
| Is this cost-sensitive, or is latency worth paying for? | Cache size, replication factor, hot versus cold storage tiers |
| Who may see this data, and is there a tenancy boundary? | Authorisation model, per-tenant partitioning, and whether a query can ever cross tenants |
| Is any of it regulated — payments, health, personal data? | Encryption at rest, audit log, retention limits, deletion propagation |
| Must a user be able to delete their data? | Whether deletes are tombstones, and how they reach caches, backups and replicas |
| Is abuse expected — bots, scraping, spam? | Rate limiting, quotas, and whether identifiers must be non-enumerable |

### Existing constraints and the environment

| Ask | What the answer changes |
|---|---|
| Is there a stack, a cloud or a database I should assume? | Whether you may pick freely or must justify against what exists |
| Which parts already exist and are off the table? | Where your design plugs in, and which boxes are just labels |
| Is there a team size or a deadline implied? | Monolith-first versus service-per-boundary, and how much you build in-house |
| Do I own the client, or is it a third-party integration? | Whether you can change protocols, add headers, or batch requests |

### LLD-specific

| Ask | What the answer changes |
|---|---|
| In-memory and single process, or persistent and distributed? | Whether a repository interface is enough, or you owe a durability story |
| Single-threaded or concurrent — can two actors act at once? | Whether locks and invariants are the closing section or absent entirely |
| What is expected to vary: rules, types, channels, policies? | Exactly which seams become a `Protocol`, and which stay plain methods |
| Which flow do you want working end to end? | Where the coding block goes; one complete path beats six skeletons |
| Is failure an exception or a returned result? | The error contract the whole design inherits, decided once |
| Do I need a command-line demo, tests, or both? | The machine-coding deliverable, and how you budget the last third |

### Defaults to state aloud when you get a shrug

| Topic | State this and move on |
|---|---|
| Scale | "I will assume 10M DAU and a few actions per user per day; correct me if that is off." |
| Peak | "Peak is 3x average; I will size for peak with headroom." |
| Read/write ratio | "10:1 for a social product, 100:1 for a link or content read path, 1:1 for chat." |
| Consistency | "Strong where money or inventory moves, eventual everywhere else, with the window named." |
| Latency | "p99 under 500 ms for an interactive read; anything slower goes asynchronous." |
| Availability | "99.9% for the v1, which is 8.76 hours a year, and I will say what four nines would cost." |
| Scope | "Analytics, admin tooling and internationalisation are out of scope for this design." |
| LLD environment | "In-memory, single process, multiple actors concurrent, persistence behind a repository." |

### Questions that waste your five minutes

| Do not ask | Because |
|---|---|
| "What database should I use?" | That is the answer you are being graded on |
| "How many servers do we have?" | You derive node counts from QPS and per-node capacity |
| Anything answerable by an assumption you could state | It spends your clock and reads as needing permission |
| A second question on a topic they already declined | They told you it is not where the marks are |

## Memory hooks

- **Eleven topics, in order: actors, scale, ratio, consistency, latency, durability, availability, geography, cost, security, constraints.** Say them as a sweep and stop where the answers change something.
- **"Ask what changes the design; assume the rest aloud."** An unstated assumption looks like an oversight; a stated one looks like judgement.
- **Every scale answer feeds one formula.** DAU times actions divided by 10^5 seconds, then times three for peak.
- **Say availability as downtime, never as nines.** 8.76 hours versus 52.6 minutes is a design conversation; "three nines versus four" is not.
- **The LLD four: persistence, concurrency, variation, deliverable.** Ask all four in the first two minutes.
- **A question you would not act on is a question you should not ask.**

!!! tip "Interview tip"
    Batch your questions and pre-commit to defaults: "Three things change the design — read/write ratio, whether the feed is ranked, and the freshness budget. My defaults are 100:1, chronological and a few seconds; tell me which to change." You get the answers and demonstrate that you know which ones matter.

!!! warning "Common mistake"
    Spending eight minutes on clarification and then designing as if the conversation never happened. Write the answers where you can see them, and check every line against the diagram before you move to deep dives. Requirement coverage is graded explicitly, and unused answers score the same as unasked questions.

## Related

- [The 45-minute HLD framework](../hld/fundamentals/interview-framework.md) — where these questions sit in minutes 0 to 5
- [The LLD interview framework](../lld/fundamentals/lld-interview-framework.md) — the same step for object-oriented rounds
- [HLD round checklist](hld-checklist.md) — what must be on the board once the answers are in
- [Back-of-envelope estimation](../hld/fundamentals/estimation.md) — turning the scale answers into four numbers
- [Latency numbers and estimation tables](latency-and-estimation.md) — the defaults quoted above
- [Design a URL shortener](../hld/case-studies/url-shortener.md) — a worked clarification table on a real prompt
