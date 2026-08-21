---
title: LLD round checklist
description: The object-oriented design round as a tick list — the clock and its variants, the deliverable per step, the Amazon OOD rubric mapping, a code-quality checklist and the test that tells you to stop adding patterns.
---
# LLD round checklist

## How to use this sheet

Run it as a pre-flight before a mock, then as a silent audit while you write. The clock is the one in [The LLD interview framework](../lld/fundamentals/lld-interview-framework.md); this page adds the deliverable, the rubric sentence and the code-quality items reviewers notice in the first thirty seconds.

## Tables

### The clock, with what must exist before you move on

| Minutes | Step | On the board before you move on | Say it out loud |
|---|---|---|---|
| 0-5 | Clarify | Numbered use cases, written assumptions, one out-of-scope line | "In-memory, single process, multiple gates concurrent; stop me if that is wrong." |
| 5-10 | Entities | Class names with attributes; each verb assigned to the class holding its data | "The ticket knows its entry time, so the ticket computes duration." |
| 10-18 | Class diagram | Structure with multiplicities; policies hung off it; the locks marked | "A lot composes floors; a ticket references one spot, and may outlive it." |
| 18-35 | Code | One flow end to end: enums, then errors, then entities, then the service method | "I will make entry work end to end before I widen anything." |
| 35-42 | Concurrency and tests | Which lock protects which state; four or five named test cases | "The floor owns the lock because the invariant is one vehicle per spot." |
| 42-45 | Extensions | Two follow-ups answered as a seam plus the test it makes possible | "A daily cap is one new pricing class and one test; nothing else moves." |

### Variants of the same shape

| Format | Budget | What the extra time buys |
|---|---|---|
| 45-minute round | 5 / 5 / 8 / 17 / 7 / 3 | nothing spare; protect the coding block |
| 60-minute round | 7 / 8 / 10 / 23 / 7 / 5 | a second flow and one real test, not more abstraction |
| Machine coding (90-120 min) | ~10 plan, 15 skeleton, 50 core, 20 tests, rest for a demo and README | a repository that runs; something must execute by the halfway mark |

### Deliverable tick list

| Step | Tick when it is true |
|---|---|
| Clarify | actors, the variation to absorb, scale, concurrency — all four asked or assumed aloud |
| Clarify | the assumption list is read back before the diagram is drawn |
| Entities | attribute-only nouns dropped; nouns with validation or arithmetic promoted to value objects |
| Entities | no class name needs the word "and" |
| Diagram | every relation carries a multiplicity, and composition is distinguished from aggregation |
| Diagram | under a dozen boxes |
| Interfaces | signatures with return types written before any body |
| Interfaces | the error contract decided once: exceptions or result objects, not both |
| Code | enums and domain errors first, then entities, then the one service method |
| Code | injected collaborators named: clock, id generator, repository, gateway |
| Concurrency | the lock, its granularity, the invariant it defends, the acquisition order |
| Tests | five cases named: happy path, validation failure, state transition, race, edge case |

### Amazon OOD rubric mapping

| Principle | What it is in this round | The sentence that scores it |
|---|---|---|
| Dive Deep | the concurrency answer, not a promise to add a lock | "The floor lock serialises two gates racing for the last compact spot; a lot-wide lock would serialise the whole building." |
| Insist on the Highest Standards | tests, error handling, naming | "Before extensions, here are the five tests I would write and the one that would have caught the double-booking." |
| Ownership | assumptions, trade-offs, restart and partial failure | "This is in-memory, so a restart loses held seats; that is why a hold carries an expiry rather than living forever." |
| Invent and Simplify | the pattern you decline as loudly as the ones you keep | "I would not make the lot a Singleton; I build one in `main` and inject it so tests can build several." |
| Are Right, A Lot | taking the hint instead of defending | "Good point — two gates can hit the same spot. Let me move the check inside the lock." |

### Code quality reviewers notice in thirty seconds

| Item | What good looks like |
|---|---|
| Type hints | every signature annotated; the union syntax rather than `Optional[X]`, never a bare `dict` |
| Value objects | `@dataclass(frozen=True, slots=True)` for `Money`, `TimeRange`, ids |
| Entities | `@dataclass(slots=True)` with behaviour on it, not a bag of fields plus a service |
| States | an `Enum` or `StrEnum`, never string literals compared with `==` |
| Interfaces | a small `Protocol` per client need; `ABC` only when there is shared behaviour |
| Errors | a domain hierarchy (`LotFullError`, `InvalidStateError`); no bare `except`, nothing swallowed |
| Money and time | integer cents or `Decimal`, never float; a clock injected, never `datetime.now()` inside a service |
| Control flow | guard clauses at the top, nesting no deeper than two, no `if` ladder over a type |
| Concurrency | one named lock per invariant, a documented acquisition order, no lock held across IO |
| Hygiene | no mutable default arguments, no module-level mutable state, `print` only in the demo |
| Tests | deterministic: fake clock, seeded randomness, no sleeps; one concurrency test per lock |
| Naming | verbs for methods, nouns for classes; no `Manager`, `Helper` or `Utils` |

### Stop adding patterns when any of these is false

| Question | Keep the pattern if | Otherwise |
|---|---|---|
| Can you name the second implementation, today? | you can name it and the requirement that asks for it | write a plain method and say where the seam would go |
| Does a test get easier? | it lets you inject a fake instead of patching | drop it; a Protocol with one implementation and no double is ceremony |
| Does the follow-up become a one-class change? | adding the variant touches only new code | drop it; the indirection buys nothing |
| How many patterns are on the board? | three or four, each justified aloud | above that, cut the weakest and say why |
| Does the diagram still fit the problem? | under a dozen boxes | delete a class before you add another |

### Red flags, and what they read as

| Red flag | Reads as | Fix in the moment |
|---|---|---|
| Announcing a pattern list in minute one | vocabulary without judgement | name the axis of change first, the pattern second |
| One `*Manager` holding everything | cannot decompose | split at the first "and" in the class name |
| Dataclasses plus a service reaching through them | anemic model | move the arithmetic onto the object that owns the data |
| Minute 32, eight classes, no method body | over-invested in structure | say so, then code the core flow with stubs around it |
| Designing tables and sharding | wrong round | "persistence sits behind a repository interface" and return to behaviour |
| "I would synchronise it somehow" | never shipped concurrent code | name the lock, the state, the granularity, the test |

## Memory hooks

- **Clarify, entities, relationships, diagram, interfaces, patterns, code, close.** Eight steps, six slots on the clock.
- **"Working code for one flow beats skeletons for six."** At minute 30 with nothing runnable, delete a class.
- **Nouns become classes, verbs become methods, and the verb lives with its data.**
- **A pattern needs a second implementation and a test.** Name both or write a method.
- **Close with the three C's: concurrency, cases, changes.** Which lock, which tests, which extensions.
- **Say the refactor, then the label.** "Each shipping rule behind one `cost` method — that is open/closed."

!!! tip "Interview tip"
    Spend the first thirty seconds announcing the plan: "I will clarify, sketch entities and relationships, draw the diagram, then code the entry flow and close with concurrency and extensions." It lets the interviewer redirect you before you spend twenty minutes on the half they do not care about, and it reads as someone who has run this on the job.

!!! warning "Common mistake"
    Gathering requirements and then designing as if the conversation never happened — three vehicle types when they said four, no locking when they said two gates operate at once. Requirement coverage is graded explicitly. Before you draw the class diagram, read the assumption list back and check that every line has a home in the design.

## Related

- [The LLD interview framework](../lld/fundamentals/lld-interview-framework.md) — the eight steps, the timeboxes and the six graded signals
- [SOLID in Python](../lld/fundamentals/solid-principles.md) — the vocabulary for the extensibility questions in the last ten minutes
- [Problem to pattern quick reference](pattern-quick-reference.md) — symptom to pattern to Python idiom, in one table
- [Clean code and testing](../lld/fundamentals/clean-code-and-testing.md) — the code-quality rows in depth
- [Concurrency for LLD in Python](../lld/fundamentals/concurrency-for-lld.md) — locks, invariants and deterministic concurrency tests
- [Design a parking lot](../lld/problems/parking-lot.md) — the whole checklist applied end to end
