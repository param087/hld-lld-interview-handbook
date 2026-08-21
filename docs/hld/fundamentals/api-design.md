---
title: API design for HLD rounds
description: How to write the three to five endpoints of a design so they survive the follow-ups — resources and status codes, versioning, keyset pagination behind opaque cursors, idempotency keys, one error envelope, 429 with Retry-After, 202 and webhooks for long work, and batch endpoints, with a tested opaque-cursor module.
---
# API design for HLD rounds

## TL;DR

- Minutes 9-14 fix the contract the rest of the design serves: nouns, verbs, status codes, and the two things interviewers always probe — an `Idempotency-Key` on writes, an opaque `cursor` on lists.
- Lists page by keyset behind an opaque cursor, retryable writes carry an idempotency key, errors share one envelope, a 429 says when to retry.
- Long work returns 202 and an operation to poll, or a signed webhook; REST at the edge, gRPC inside, GraphQL only where clients shape the response.

## Core concepts

The interviewer is not grading HTTP trivia; they are checking that the contract carries the load you just estimated and survives the retries, duplicates and partial failures of the deep dives. Write three to five endpoints in two minutes, then spend the rest on pagination, idempotency, errors and versioning.

### Resources, verbs and status codes

Model nouns, not actions: `/v1/orders`, `/v1/orders/{id}`, `/v1/orders/{id}/items`. Collections are plural, ids are opaque rather than guessable counters, nesting stops at one level, and anything that wants to be a verb becomes a sub-resource — `POST /v1/orders/{id}/cancellation`, not `POST /cancelOrder`. The verbs carry semantics the whole infrastructure understands: GET is safe and cacheable, PUT and DELETE are idempotent by definition, PATCH is partial, POST is neither until you make it so. Status codes are the same kind of contract: 200 read or update, 201 plus `Location` for a creation, 202 accepted, 204 deleted; 400 malformed, 401 unauthenticated, 403 forbidden, 404 missing, 409 conflicts with state, 422 breaks a rule, 429 rate limited; 500 your bug, 503 a dependency down. Clients, gateways and dashboards branch on these numbers, so a 200 wrapping `{"error": ...}` breaks retries, caching and alerting at once.

### Versioning: path or header

Version only to break a client: a removed or renamed field, a changed type, changed semantics. Adding optional fields is not a break, provided the contract tells clients to ignore unknown keys. A path version (`/v1/orders`) is visible in every log, cacheable by URL and routable at the gateway; a header version (`Accept: application/vnd.shop.v2+json`) keeps URLs stable but is invisible to proxies and to curl. Choose the path, run at most two major versions, and announce retirement with `Deprecation` and `Sunset` headers.

### Pagination: offset, keyset and opaque cursors

`?page=50&limit=20` becomes `OFFSET 980 LIMIT 20`: the database reads and discards 980 rows to return 20, page 5,000 reads 100,000, and a row inserted between requests shifts everything by one, so the reader sees a duplicate or a gap. Offset paging suits small, static lists where a client must jump to page N, and nothing else.

Keyset (seek) pagination remembers where the last page ended: `WHERE (created_at, id) < (?, ?) ORDER BY created_at DESC, id DESC LIMIT 21`. With an index on `(created_at, id)` that is one seek and 21 rows whatever the page number, and an insert after the boundary cannot move what lies before it. The sort key must be total, which is why the id joins the timestamp: orders sharing a millisecond would otherwise be skipped or repeated. The extra row proves a next page exists without a `COUNT(*)`.

Expose the boundary as an opaque cursor: base64 of the key (`created_at|id`) plus a fingerprint of the filter and sort it belongs to, HMAC-signed so nobody edits it. Opacity lets you change the key later, stops clients forging positions, and rejects a cursor replayed against another filter. Return `next_cursor: null` on the last page and never promise a total count on a hot path — [Design a news feed](../case-studies/news-feed.md) uses this shape exactly.

Filters and sorts are query parameters over indexed columns (`?customer_id=42&status=shipped&sort=-created_at`): allow sorting only on fields indexed with the tie-break id, since a keyset page on an unindexed sort is a full scan, and reject unknown fields with a 400 naming them. Both belong in the cursor's fingerprint, so nobody changes the filter mid-walk.

**Choose the pagination scheme from the access pattern, then decide what the cursor carries.**

```mermaid
flowchart TD
    p_start["A list endpoint"] --> q_jump{"Client must jump to<br/>an arbitrary page?"}
    q_jump -->|"yes, list is small and static"| p_offset["Offset + limit:<br/>OFFSET n reads n rows first"]
    q_jump -->|"no"| q_sort{"Sort key unique<br/>and indexed?"}
    q_sort -->|"yes"| p_keyset["Keyset: seek past the boundary,<br/>LIMIT n+1 to detect the next page"]
    q_sort -->|"no"| p_fix["Append the id as tie-break,<br/>index (sort_key, id)"]
    p_fix --> p_keyset
    p_keyset --> q_rank{"Ranked or search results<br/>that change between pages?"}
    q_rank -->|"yes"| p_snap["Cursor carries score + id,<br/>or a snapshot id"]
    q_rank -->|"no"| p_key["Cursor carries (created_at, id)"]
    p_snap --> p_opaque["Encode: base64(key + query fingerprint),<br/>HMAC-signed, next_cursor null at the end"]
    p_key --> p_opaque
    p_offset --> p_total["Total count only if it is cheap"]
```

### Idempotency keys on POST

A client that times out on `POST /v1/orders` cannot know whether the order exists; it either retries and creates a duplicate or gives up and loses the sale. The fix is a client-generated `Idempotency-Key` header, one UUID per logical operation, stored with a fingerprint of the request and the response it produced. A retry with the same key and body replays that response; the same key with a different body is a client bug, answered 422; a key whose first attempt is still running gets 409, so concurrent twins cannot both create. Scope keys per caller, and expire them after a retention window, commonly 24 hours. PUT and DELETE need no key: a repeat is harmless by definition. [Transactions, 2PC, sagas and idempotency](transactions-and-distributed-transactions.md) builds the store.

**A retried POST with an Idempotency-Key: a concurrent twin is refused, the lost response is replayed, nothing is created twice.**

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant S as Order service
    participant I as Idempotency store
    participant D as Orders DB
    C->>S: POST /v1/orders, Idempotency-Key k1, body b
    S->>I: begin(caller, k1, fingerprint(b))
    I-->>S: NEW, claim token
    opt a concurrent twin arrives while the first attempt runs
        C->>S: POST /v1/orders, Idempotency-Key k1, body b
        S->>I: begin(caller, k1, fingerprint(b))
        I-->>S: IN_PROGRESS
        S-->>C: 409 Conflict, Retry-After 1
    end
    S->>D: insert order 123
    D-->>S: ok
    S->>I: complete(k1, token, 201 with order 123)
    S-->>C: 201 Created, lost on the way (client timed out)
    C->>S: retry POST /v1/orders, Idempotency-Key k1, body b
    S->>I: begin(caller, k1, fingerprint(b))
    I-->>S: REPLAY, stored 201
    S-->>C: 201 Created, order 123, no second insert
    Note over C,D: same key with a different body is answered 422, a client bug
```

### The error envelope

Every error has one shape, so clients write one handler: `{"error": {"code": "ORDER_NOT_CANCELLABLE", "message": "...", "details": [...], "request_id": "..."}}`, or RFC 9457's `application/problem+json`. The stable part is the machine-readable `code` clients branch on; the message is for humans. Include the request id so a support ticket traces through the logs, field-level details for validation failures, and never a stack trace or internal hostname.

### 429, Retry-After and X-RateLimit headers

A rejection must stop the retry storm: status 429, `Retry-After` in seconds, and `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` on every response so well-behaved clients slow down before they are cut off. A 503 with `Retry-After` is the same contract for an overloaded backend. Without it, a thousand clients retrying once a second turn one breach into a sustained 1k QPS — a whole app server's capacity. [Rate limiting](rate-limiting.md) covers the algorithms.

Those limits, like TLS termination, authentication, size and timeout caps, CORS and `/v1` versus `/v2` routing, are gateway policies, not service code — [Load balancing, reverse proxies and API gateways](load-balancing-and-api-gateway.md) has the duty list and prices the hop.

### Long-running operations: 202 and poll, or webhook

Work that outlives a client's patience — a transcode, a report, a bulk import — must not hold the connection. Return `202 Accepted` with an operation resource, `GET /v1/operations/{id}`, carrying a status (pending, running, succeeded, failed), progress and, on success, the result's location. The client polls at the interval you suggest in `Retry-After`, and the document is cheap to serve from cache. Polling costs empty reads — 100k operations polled every 5 s is 20k QPS of "still running" — so offer a webhook too: browsers poll, partners subscribe.

**Long-running work: 202 with an operation to poll; a registered webhook replaces the polling for server-to-server clients.**

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as API
    participant Q as Queue
    participant W as Worker
    participant H as Client webhook endpoint
    C->>A: POST /v1/reports, Idempotency-Key k7
    A-)Q: enqueue job op9
    A-->>C: 202 Accepted, Location /v1/operations/op9, Retry-After 5
    Q-->>W: job op9
    loop until the status is terminal
        C->>A: GET /v1/operations/op9
        A-->>C: 200 running, progress 40 percent, Retry-After 5
    end
    W->>A: op9 succeeded, result /v1/reports/r1
    alt webhook registered
        A->>H: POST event report.completed, id evt1, timestamp, HMAC signature
        H-->>A: 2xx within 5 s, otherwise retry with backoff
    else polling only
        C->>A: GET /v1/operations/op9
        A-->>C: 200 succeeded, result /v1/reports/r1
    end
```

### Webhooks: retries, signatures, ordering

A webhook is your server calling theirs, so every client failure mode is now yours. Deliver at least once: retry with exponential backoff and jitter for hours, then park the event in a dead-letter list the customer can replay. Every event therefore carries a unique id, and the contract states that receivers deduplicate. Sign every payload — HMAC-SHA256 over timestamp and body with a per-endpoint secret, verified in constant time with a five-minute replay tolerance. Never promise ordering across retries: carry a sequence number or resource version so receivers drop stale events.

### Batch endpoints

When a client needs 100 orders by id, 100 GETs cost 100 round trips: 100 x 500 µs = 50 ms inside a datacenter, 100 x 70 ms = 7 s from a phone across the US. Offer `GET /v1/orders?ids=1,2,...` capped at 100 ids, or `POST /v1/orders:batchGet` when the list outgrows a URL. Batch writes return per-item results, because partial failure is normal and one 400 for the whole batch forces a full retry. Bound every batch; make batch writes idempotent per item.

## Trade-offs

| Style | Contract | Payload | Browsers and proxies | Caching | Streaming | Evolution | Use it for |
|---|---|---|---|---|---|---|---|
| REST + JSON | Conventions, OpenAPI optional | Text, self-describing, largest | Native everywhere, curl-debuggable | By URL at CDN and gateway | SSE or chunked responses | Additive fields, path versions | Public APIs, the edge |
| gRPC + protobuf | `.proto` file, generated stubs | Binary, compact, fast to parse | Needs gRPC-Web or a gateway | None by URL | Client, server, bidirectional | Field numbers, reserved ids | Between services |
| GraphQL | Typed schema, client picks fields | JSON shaped per query | One POST endpoint | Persisted queries only | Subscriptions over WebSocket | Deprecate fields, no versions | Client-driven UIs |

Default to REST with JSON at the edge: every client, proxy, CDN and debugger already speaks it, GET responses cache by URL, and the conventions above make the contract predictable for people who never read your docs. Move to gRPC between services once payload size, parse cost and a typed `.proto` contract pay for themselves, keeping a REST or gRPC-Web gateway for anything a browser calls. Reach for GraphQL when many client screens want different shapes of the same data and the mobile team is paying in round trips — 150 ms each between California and the Netherlands — accepting that you now own a query planner, batching loaders, cost limits and a cache that no longer keys by URL. [Networking for system design](networking-essentials.md) prices all three on the wire. Whatever the style, the asynchronous half is the same: 202 with an operation to poll, signed at-least-once webhooks, idempotency keys wherever a client might retry. Pick one style per boundary and say why.

## Python implementation

The cursor is the last row's sort key plus a fingerprint of the query, encoded as `base64url(payload).base64url(hmac)`; `decode` rejects anything the server did not sign:

```python title="code/hld/cursor_pagination.py — opaque, signed cursor"
--8<-- "code/hld/cursor_pagination.py:cursor"
```

`OrderTable` keeps rows sorted by `(created_at, id)`, one list per customer as a composite index; `page_by_keyset` is one `bisect` and a slice of `limit + 1` rows, `page_by_offset` the naive scan kept for comparison, and `_lock` keeps pages consistent under concurrent inserts:

```python title="code/hld/cursor_pagination.py — keyset and offset pages"
--8<-- "code/hld/cursor_pagination.py:table"
```

`walk_keyset` and `walk_offset` page the whole table and total what the engine read; the demo inserts a row between pages:

```python title="code/hld/cursor_pagination.py — walking every page"
--8<-- "code/hld/cursor_pagination.py:walk"
```

`uv run python -m hld.cursor_pagination` prints:

```text
table: 10,000 orders, 3 per second, newest first by (created_at, id)
keyset page 1: ord-10000 .. ord-09981, 21 rows examined
  next_cursor = WzE3MDAwMDMzMjcwMDAsIm9yZC0w... (68 chars, base64url payload + HMAC tag)
a new order ord-10001 arrives before page 2 is requested
keyset page 2: ord-09980 .. ord-09961  (continues after ord-09981: no repeat, no skip)
offset page 2: ord-09981 .. ord-09962  (repeats ord-09981: every row shifted by one)
offset 9,000: 9,020 rows examined to return 20
full walk, keyset: 501 pages, 10,501 rows examined, 0 duplicates
full walk, offset: 501 pages, 2,515,001 rows examined, 0 duplicates
filter customer_id=ann: ord-10001, ord-09999, ... (21 rows examined through the per-customer index)
ann's cursor without the filter -> 400 cursor belongs to a different query
cursor with one edited character -> 400 cursor signature mismatch
page number sent as a cursor -> 400 malformed cursor
```

Both walks return the same rows, but offset examined 2,515,001 against keyset's 10,501 — about 240x — and the last three lines are the 400s for a cursor from another query, an edited cursor and a page number.

## In the interview

Write the endpoints as a table in minute nine and state the conventions once: "Resources are plural nouns under `/v1`; every retryable write takes an `Idempotency-Key`; every list returns an opaque keyset `next_cursor`; errors share one envelope with a machine-readable code; the gateway answers 429 with `Retry-After`." That answers most follow-ups before they are asked.

Phrases that signal depth: "offset reads and discards, keyset seeks"; "the idempotency key is scoped per caller, stored with a fingerprint of the body"; "202 plus an operation resource, and a signed webhook for partners".

??? question "Why an opaque cursor rather than `?after_id=123`?"
    `after_id` works until the sort changes or the key becomes compound, and then every client breaks. An opaque cursor hides the key, carries the filter fingerprint and is signed, so nobody pages into another tenant's data by editing it.

??? question "How does the server tell a legitimate retry from a duplicate order?"
    By the `Idempotency-Key`: same key and body replays the stored response, same key with a different body is a client bug (422), a new key is a new order. Without the header there is no way to tell, so it is mandatory on POST.

??? question "A webhook receiver was down for an hour. What do they see when it comes back?"
    Your retries with backoff, possibly out of order and duplicated, each carrying an event id and a signature. They deduplicate by id, verify the signature inside the timestamp tolerance, and apply by version. Exhausted events wait in a dead-letter list.

??? question "When would you put GraphQL in this design?"
    Only in front of client-facing aggregation: several screens reading different shapes of the same entities, with a mobile team paying for round trips — and then with depth and cost limits, persisted queries and batching loaders. Never between services.

??? question "How do you version a breaking change to `/v1/orders`?"
    Ship `/v2/orders` alongside, route both at the gateway, mark v1 with `Deprecation` and `Sunset` headers, measure v1 traffic per client, and remove it after the announced window. Additive changes need no version.

!!! tip "Interview tip"
    Put `Idempotency-Key` and `cursor` in the endpoint table before anyone asks. Interviewers read the API step in seconds, and those two words say you have handled retries and pagination in production; their absence is the first "what if".

## Common mistakes

- **Verbs in URLs and 200 for everything**: `POST /getOrders` returning `200 {"error": ...}` defeats caching, retries and alerting. Fix: nouns, verbs, real status codes.
- **Offset pagination on a live list**: duplicates and skips as rows arrive, and `OFFSET 100000` reads 100,000 rows. Fix: keyset behind an opaque cursor, `limit + 1` to find the end.
- **A raw sort key as the cursor**: `?after=2024-05-01T10:00:00Z` skips rows sharing the timestamp and breaks when the sort changes. Fix: a compound key with the id, signed.
- **Retries without idempotency keys**: a client timeout becomes a second charge. Fix: a mandatory `Idempotency-Key` on POST, stored with a fingerprint and response.
- **Webhooks without signatures or dedup**: anyone can post a fake "payment succeeded", and retries double-apply. Fix: HMAC with a timestamp, unique event ids, at-least-once stated.

!!! warning "Common mistake"
    Holding the connection open for work that outlives a request: a 30-second transcode behind `POST /v1/videos` ties up a thread per upload, times out at the load balancer, and the client retries the whole upload. Return 202 with an operation resource, suggest the poll interval with `Retry-After`, and offer a webhook for server-to-server callers.

## Self-check

??? question "What does `OFFSET 980 LIMIT 20` cost, and what replaces it?"
    The engine reads 1,000 rows and discards 980. Keyset replaces it: `WHERE (created_at, id) < (?, ?) ... LIMIT 21`, one index seek whatever the page.

??? question "Why does the cursor carry the id as well as the timestamp?"
    Timestamps collide; the id makes the key total, so a boundary between rows sharing a timestamp is unambiguous — nothing skipped or repeated.

??? question "Which status codes answer an idempotency-key retry, a twin in flight, and a key reused with a different body?"
    The stored response (usually 201) for the retry, 409 for the twin, 422 for a mismatch.

??? question "What must a 429 response carry, and why?"
    `Retry-After` plus the `X-RateLimit-*` headers, so clients back off with jitter at the time you choose instead of retrying every second and turning one breach into sustained load. A 503 carries the same header.

??? question "Name three properties of a production webhook."
    At-least-once delivery with backoff and dead-letter replay, an HMAC signature over timestamp and body, and unique event ids with a version so receivers deduplicate and drop stale events.

## Related

- [Networking for system design](networking-essentials.md) — REST vs gRPC vs GraphQL on the wire, HTTP/2, round-trip costs
- [Rate limiting](rate-limiting.md) — the algorithms behind 429 and its headers
- [Transactions, 2PC, sagas and idempotency](transactions-and-distributed-transactions.md) — the idempotency store
- [Interfaces, contracts and service APIs in LLD](../../lld/fundamentals/interfaces-and-contracts.md) — the same contracts at class level
- [Load balancing, reverse proxies and API gateways](load-balancing-and-api-gateway.md) — where gateway policies run
- [Security essentials](security-essentials.md) — authentication, API keys and signed requests
- IETF RFC 9110, "HTTP Semantics" (2022) — methods, status codes, Retry-After
- IETF RFC 9457, "Problem Details for HTTP APIs" (2023)
- IETF HTTPAPI working group, "The Idempotency-Key HTTP Header Field" (Internet-Draft)
