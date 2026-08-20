---
title: HLD + LLD Interview Handbook
description: System design and low-level design for SDE2 interviews — 150+ pages, 300+ diagrams, and 100+ tested Python implementations.
---
# HLD + LLD Interview Handbook

**Everything an SDE2 candidate needs for system-design (HLD) and object-oriented-design (LLD) rounds at FAANG-style companies — in one place, with diagrams you can redraw and Python you can run.**

Every page follows the same spine an interviewer expects, every diagram is drawn to be reproduced on a whiteboard, and every code snippet is embedded from a file that the test suite runs. Numbers come from one canonical [estimation sheet](cheatsheets/latency-and-estimation.md), so the arithmetic in every case study is traceable.

<div class="grid cards" markdown>

-   :material-server-network:{ .lg .middle } __HLD fundamentals__

    ---

    27 pages: the 45-minute framework, estimation, caching, databases, replication, partitioning, transactions, CAP, Kafka, consensus, rate limiting, resilience, security, observability, geospatial indexing and more — each with a tested Python implementation of the core idea.

    [:octicons-arrow-right-24: Fundamentals](hld/fundamentals/index.md)

-   :material-city-variant-outline:{ .lg .middle } __HLD case studies__

    ---

    31 systems: URL shortener, news feed, chat, YouTube, Uber, Ticketmaster, payments, Kafka-like queue, S3, Google Docs, stock exchange… Requirements, estimation, API, data model, architecture, 3–5 deep dives, scaling, trade-offs, pacing.

    [:octicons-arrow-right-24: Case studies](hld/case-studies/index.md)

-   :material-shape-outline:{ .lg .middle } __LLD fundamentals and patterns__

    ---

    OOP in Python, SOLID, UML with Mermaid, the LLD interview framework, concurrency, testing — plus all 23 GoF patterns and 8 modern ones, each with the canonical interview example and the Pythonic shortcut.

    [:octicons-arrow-right-24: Fundamentals](lld/fundamentals/index.md) · [:octicons-arrow-right-24: Patterns](lld/patterns/index.md)

-   :material-code-braces:{ .lg .middle } __LLD problems__

    ---

    36 problems: parking lot, elevator, vending machine, LRU/LFU cache, Splitwise, BookMyShow, chess, text editor, payment wallet, pub/sub… Each a runnable package with tests, class/sequence/state diagrams and a 45-minute walkthrough.

    [:octicons-arrow-right-24: Problems](lld/problems/index.md)

-   :material-table-large:{ .lg .middle } __Cheatsheets__

    ---

    Latency numbers, database and queue selection matrices, consistency tables, pattern quick reference, HLD/LLD checklists, the 40 most common SDE2 mistakes, clarifying questions, glossary.

    [:octicons-arrow-right-24: Cheatsheets](cheatsheets/index.md)

-   :material-account-voice:{ .lg .middle } __Mock interviews and roadmap__

    ---

    Six full 45-minute transcripts (news feed, chat, Ticketmaster, parking lot, elevator, movie booking) with the diagram evolving v1 → v3 and a debrief rubric — plus an 8-week plan and a 1-week crash plan.

    [:octicons-arrow-right-24: Mocks](mocks/index.md) · [:octicons-arrow-right-24: Roadmap](roadmap.md)

</div>

## How to use it

- **Eight weeks out:** follow the [8-week roadmap](roadmap.md#8-week-plan). P0 pages come first, every week ends with something you produce from memory.
- **One week out:** the [crash plan](roadmap.md#1-week-crash-plan) — P0 fundamentals, nine case studies, eight problems, four mocks.
- **Targeting a company:** Amazon leans on LLD/OOD rounds and the [LLD framework](lld/fundamentals/lld-interview-framework.md); Meta and Google lean on product/infra HLD and the [45-minute framework](hld/fundamentals/interview-framework.md). Both expect you to quantify, so start with [estimation](hld/fundamentals/estimation.md).
- **The night before:** the [HLD checklist](cheatsheets/hld-checklist.md), the [LLD checklist](cheatsheets/lld-checklist.md) and [common mistakes](cheatsheets/common-mistakes-sde2.md).

## What makes the pages trustworthy

- **Tested code.** Every snippet is included from `code/` at build time — the same files `pytest` runs. If the page shows it, it executes.
- **One set of numbers.** Every estimation cites the [latency and estimation tables](cheatsheets/latency-and-estimation.md).
- **Diagrams you can redraw.** Mermaid flowcharts, sequence, class, state and ER diagrams kept under ~25 nodes — whiteboard-sized on purpose.
- **Interview-first structure.** Clarifying questions, follow-ups with model answers, common mistakes and minute-by-minute pacing on every case study and problem.

The source is on [GitHub](https://github.com/param087/hld-lld-interview-handbook) under the MIT license.
