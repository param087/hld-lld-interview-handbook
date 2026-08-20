---
title: T_TITLE
description: T_ONE_SENTENCE
---
<!-- T: Read AUTHORING.md first. Delete every "T:" comment. Keep the H2 headings exactly as written, in this order. Use "Deep dive: <crux>" H2s (3-5 of them). -->
# T_TITLE

## TL;DR
<!-- T: The system in 3 bullets + the 3-5 cruxes the interviewer will probe. <=100 words. -->

## Problem statement and clarifying questions
<!-- T: 1 paragraph framing, then 6-10 clarifying questions with the assumed answers (table: question | assumption). -->

## Requirements
<!-- T: Numbers in non-functional requirements (scale, latency targets, consistency, durability, availability). -->

### Functional

### Non-functional

### Out of scope

## Estimation
<!-- T: Show the arithmetic. Table: read QPS, write QPS, storage/year, bandwidth, cache size. Use ONLY numbers from docs/cheatsheets/latency-and-estimation.md. -->

## API design
<!-- T: 3-6 endpoints with request/response shapes, idempotency and pagination notes. -->

## Data model
<!-- T: erDiagram + store choice + partition/sort keys + indexes. -->

## High-level design
<!-- T: v1 architecture flowchart (subgraphs: Clients / Edge / Services / Async / Data). Then the write path and the read path, each as a sequenceDiagram with narration. -->

## Deep dive: T_CRUX_1
<!-- T: The probing question, options table, chosen approach and why, diagram or the key Python snippet. 250-400 words each. Repeat for each crux (3-5 H2s). -->

## Deep dive: T_CRUX_2

## Deep dive: T_CRUX_3

## Scaling, bottlenecks and failure modes
<!-- T: v2 flowchart (sharding, replicas, queues, caches, multi-region as relevant). What breaks first; degradation behaviour; hot spots. -->

## Trade-offs summary
<!-- T: Table: decision | chosen | alternatives | why. -->

## Interviewer follow-ups
<!-- T: 6-8 Q&A using `??? question "..."`. Include exactly one `!!! tip "Interview tip"` and one `!!! warning "Common mistake"` somewhere on the page. -->

## 45-minute pacing
<!-- T: Table: minutes | what to say/draw for THIS problem. -->

## Related
<!-- T: 3-6 relative links to catalogue pages (fundamentals it builds on, sibling case studies, LLD problems). -->
