---
title: Networking for system design
description: The networking an HLD round actually tests — DNS, TCP vs UDP, the TLS 1.3 handshake, HTTP/1.1 vs HTTP/2 vs HTTP/3, REST vs gRPC vs GraphQL, the four realtime transports, connection pooling and serialization — priced in round trips so you can reason about latency on the whiteboard.
---
# Networking for system design

## TL;DR

- Every hop is a round trip you can price: ~500 µs inside a datacenter, 70 ms across the US, 150 ms California to the Netherlands; connection setup costs one to three of them before the first byte.
- Know what each layer buys: DNS steers, TCP orders, TLS 1.3 secures in one round trip, HTTP/2 multiplexes, QUIC removes head-of-line blocking, pooling amortises all of it.
- Interviewers probe realtime transports (polling, long polling, SSE, WebSocket) and API styles (REST, gRPC, GraphQL); choose by direction, latency and who sits between client and server.

## Core concepts

Networking questions in a design round are latency questions in disguise: how many round trips does a request cost, where are they, and what removes them. Keep three numbers in front of you — a same-datacenter round trip is ~500 µs, a US coast-to-coast round trip ~70 ms, California to the Netherlands and back ~150 ms — and every protocol below becomes a count of those.

### DNS: recursion, TTL, GeoDNS and anycast

**A cold DNS lookup walks root, TLD and authoritative servers; a warm one is a cache hit at the recursive resolver.**

```mermaid
sequenceDiagram
    autonumber
    participant C as Client stub resolver
    participant R as Recursive resolver (ISP or public)
    participant Root as Root name server
    participant TLD as TLD name server (.com)
    participant Auth as Authoritative server (example.com)
    C->>R: A api.example.com?
    alt cached and TTL not expired
        R-->>C: 203.0.113.10 (from cache)
    else cache miss
        R->>Root: A api.example.com?
        Root-->>R: referral to .com servers
        R->>TLD: A api.example.com?
        TLD-->>R: referral to example.com name servers
        R->>Auth: A api.example.com?
        Note over Auth: GeoDNS picks the answer by resolver location
        Auth-->>R: 203.0.113.10, TTL 60 s
        R-->>C: 203.0.113.10
    end
    Note over C,R: the client reuses the answer until the TTL expires
```

The client's stub resolver asks a recursive resolver, which answers from cache or walks the hierarchy: root servers refer it to the TLD servers, which refer it to your authoritative servers, which answer. Each referral is a round trip, so a cold lookup from Europe against US-hosted name servers can cost several hundred milliseconds, which is why resolvers cache aggressively and why every record carries a TTL. The TTL is a design knob: a 60-second TTL lets you move traffic off a failed region within a minute or two but keeps resolvers coming back, a one-day TTL makes a failover take a day for some clients. GeoDNS returns a different answer per resolver location, steering users to the nearest region; it is approximate because it sees the resolver's location, not the user's. Anycast announces one IP from many locations and lets routing deliver each packet to the nearest one — how public resolvers and CDN edges work, and a clean way to spread load balancer traffic across sites without DNS tricks.

### TCP vs UDP: handshakes and head-of-line blocking

TCP gives you an ordered, reliable byte stream for the price of a three-way handshake — one round trip before any data — plus congestion control that starts slow and backs off on loss. Its weakness for modern workloads is head-of-line blocking: one lost segment stalls every byte behind it, even bytes that belong to unrelated requests multiplexed on the same connection. UDP is datagrams with no handshake, no ordering and no retransmission; anything you need on top is yours to build, which is exactly what QUIC does. Use TCP for almost everything; reach for UDP when a late packet is worthless (voice, video, game state) or when you are building a transport that needs independent streams.

### TLS 1.3: one round trip to a secure channel

**TLS 1.3 completes in one round trip after TCP; resumption can send data in the first flight.**

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant S as Server
    Note over C,S: TCP handshake first (SYN, SYN-ACK, ACK): 1 RTT
    C->>S: ClientHello: TLS 1.3, cipher suites, ECDHE key share, SNI, ALPN
    S-->>C: ServerHello: chosen suite, server key share
    Note over S: both sides derive the handshake keys now
    S-->>C: EncryptedExtensions, Certificate, CertificateVerify, Finished
    C->>S: Finished, then application data
    Note over C,S: 1 RTT for TLS 1.3, 2 RTT for TLS 1.2
    C->>S: HTTP request
    S-->>C: HTTP response
    opt session resumption with a pre-shared key
        C->>S: ClientHello with PSK and 0-RTT early data
        S-->>C: ServerHello, Finished, response
    end
```

TLS 1.3 folds key exchange into the first flight: the client guesses the key-exchange group and sends its share with the ClientHello, the server answers with its own share and can encrypt everything after the ServerHello, so the handshake costs one round trip instead of TLS 1.2's two. Priced from California to Amsterdam, a fresh TLS 1.2 connection is TCP plus two TLS round trips, 3 x 150 ms = 450 ms before the first byte; TLS 1.3 is 300 ms; QUIC merges the transport and TLS handshakes into one, 150 ms; and resumption with 0-RTT data puts the request in the first packet. The catch with 0-RTT is replay — an attacker can resend the early data — so only idempotent requests may ride on it. SNI tells the server which certificate to present, ALPN negotiates HTTP/2 or HTTP/3 during the handshake, and TLS termination at the load balancer is where these costs are paid in most designs, with plaintext or mutual TLS behind it.

### HTTP/1.1, HTTP/2 and HTTP/3

| | HTTP/1.1 | HTTP/2 | HTTP/3 |
|---|---|---|---|
| Transport | TCP, one request at a time per connection | TCP, many streams on one connection | QUIC over UDP, independent streams |
| Concurrency | Browsers open a handful of connections per host | Multiplexing, header compression, prioritisation | Same, without transport-level head-of-line blocking |
| Head-of-line blocking | Per connection, at the HTTP layer | Removed at HTTP, remains at TCP: one lost packet stalls all streams | Removed: a lost packet stalls only its stream |
| Connection setup | TCP + TLS | TCP + TLS | One handshake, 0-RTT resumption, survives IP changes |
| Use it for | Legacy clients, simple internal calls | The default for browsers and gRPC | Mobile and lossy networks, CDN edges |

HTTP/1.1 keep-alive reuses a connection for sequential requests, which removes the handshakes but still serialises requests, so browsers open several connections per host to get parallelism. HTTP/2 multiplexes many streams on one connection with compressed headers, which is why it is the default for browsers and the transport under gRPC, but all streams share one TCP connection, so a single lost packet stalls every stream until it is retransmitted — worse, on a lossy link, than HTTP/1.1's separate connections. HTTP/3 runs on QUIC, where each stream is delivered independently and a connection is identified by an ID rather than the address tuple, so a phone moving from Wi-Fi to cellular keeps its connection. In a design, say HTTP/2 between services and at the edge, HTTP/3 where you serve mobile users over poor networks.

### REST vs gRPC vs GraphQL

REST is resources and HTTP verbs over JSON: cacheable by URL, debuggable with curl, understood by every proxy and client. gRPC is typed RPC over HTTP/2 with protobuf: a schema you compile into client stubs, streaming in both directions, and messages several times smaller and faster to parse than JSON, at the price of opaque binary payloads and a gateway whenever a browser must call it. GraphQL gives the client one endpoint and a query language to ask for exactly the fields it needs across several resources, which removes the mobile client's round trips and over-fetching and moves the cost to the server, where an unbounded query can fan out into hundreds of database calls and where per-URL caching no longer works. The usual split: REST at the public edge, gRPC between services, GraphQL as a client-facing aggregation layer when many screens want different shapes of the same data. [API design for HLD rounds](api-design.md) covers the conventions for each.

### Realtime: short polling, long polling, SSE and WebSocket

**Choose the realtime transport by update frequency, direction and what sits between client and server.**

```mermaid
flowchart LR
    r_start["Server has updates for a client"] --> q_freq{"Updates rarer than<br/>about once a minute?"}
    q_freq -->|"yes"| r_short["Short polling:<br/>GET every N seconds, cache-friendly"]
    q_freq -->|"no"| q_dir{"Client sends frequent<br/>messages too?"}
    q_dir -->|"yes"| r_ws["WebSocket:<br/>full duplex over one TCP connection"]
    q_dir -->|"no: server to client only"| q_proxy{"Must traverse strict proxies<br/>or HTTP/1.1-only clients?"}
    q_proxy -->|"yes"| r_long["Long polling:<br/>request parks until data or timeout"]
    q_proxy -->|"no"| r_sse["SSE:<br/>one-way text stream over HTTP, auto-reconnect"]
    r_ws -.->|"upgrade blocked"| r_long
    r_sse -.->|"no EventSource support"| r_long
    r_ws --> r_scale["Stateful tier: session registry,<br/>pub/sub fan-out, drain on deploy"]
    r_sse --> r_scale
    r_long --> r_scale
```

Short polling asks on a timer and is the right answer more often than candidates admit: it is stateless, cacheable and works everywhere, and its cost is empty responses — 1M clients polling every 5 seconds is 200k QPS of mostly nothing, ~20 proxy-class nodes at ~10k QPS each. Long polling parks the request on the server until there is data or a timeout, then the client immediately asks again: near-instant delivery through any proxy, at the price of a held connection per client and a reconnect per message. SSE keeps one HTTP response open and streams text events server to client with built-in reconnection and last-event IDs; it is the simplest push transport and multiplexes cleanly over HTTP/2. WebSocket upgrades an HTTP connection to a full-duplex binary channel for chat, collaborative editing and games; it costs an upgrade that some corporate proxies block, so plan a long-polling fallback. Every push transport makes the server stateful — a connection lives on one node — so scaling needs a session registry (which node holds this user), a pub/sub layer to route a message from the node that received it to the node that holds the recipient, heartbeats to detect dead connections, and a deploy process that drains connections instead of dropping them; [Design a chat system](../case-studies/chat-messenger.md) builds that tier for 50M DAU and ~23k messages/s.

### Connection pooling

Inside a datacenter a new connection per request costs a TCP round trip plus a TLS round trip, 2 x 500 µs = 1 ms, plus the CPU of the key exchange — more than many requests take to execute. Pools keep warm connections to each downstream (databases, caches, other services) and hand them out per request, so the steady-state cost of a hop is one round trip. Size pools deliberately: a database accepts only so many connections, so 200 app servers with a pool of 50 each is 10,000 connections against a primary that may want a few hundred, which is why a connection proxy (PgBouncer-style) sits between the app tier and a relational primary. Set connect and read timeouts on every pool, because a hung connection held forever is how one slow dependency exhausts every caller.

### Serialization and schema evolution

JSON is self-describing text: readable, schemaless, universally supported, and large and slow to parse relative to binary formats. Protobuf is a compiled schema with numbered fields: compact, fast and strongly typed, with evolution rules that make it safe to change — add new fields with new numbers, never reuse or renumber a field, keep unknown fields when forwarding. Avro stores the writer's schema alongside the data (or in a registry) and resolves reader and writer schemas at read time, which suits event logs where producers and consumers upgrade at different times. The evolution rules are the interview content: a new required field breaks old producers, a removed field breaks old consumers, so make every change additive, give every field a default, and put a schema registry in front of any topic that more than one team reads.

## Trade-offs

| Transport | Delivery latency | Server cost per client | Direction | Proxies and HTTP/2 | Fallback | Typical use |
|---|---|---|---|---|---|---|
| Short polling | Up to the poll interval | A request every N seconds, mostly empty | Client pull | Works everywhere, cacheable | None needed | Dashboards, order status, anything that changes every minute or slower |
| Long polling | Near instant | One parked connection plus a reconnect per message | Server push, one message at a time | Works everywhere | Is the fallback | Notifications behind strict proxies |
| SSE | Near instant | One open response, cheap | Server to client only | Plain HTTP, multiplexes on HTTP/2 | Long polling | Feeds, tickers, progress, LLM token streams |
| WebSocket | Lowest, binary frames | One upgraded connection with heartbeats | Full duplex | Upgrade can be blocked, one stream per connection | Long polling | Chat, collaborative editing, games, trading |

Start from the slowest transport that meets the latency requirement, because each step up adds server state. If updates arrive every minute or slower, poll: it is stateless, it caches at the CDN, and the interviewer will respect the restraint. If the server pushes and the client rarely talks back — feeds, notifications, progress bars, streamed model output — SSE is the default: one open HTTP response, automatic reconnection with a last-event ID, no special proxy handling on HTTP/2. Choose WebSocket only when the client sends as often as it receives or needs binary frames with minimal framing overhead, and say in the same breath how you will scale the stateful tier: a registry of which node holds which connection, a pub/sub bus to route between nodes, heartbeats, and draining on deploy. Keep long polling as the fallback for both push transports, since corporate proxies still strip upgrades and some clients lack EventSource. For APIs, the same restraint applies: REST unless you need the typed contract and streaming of gRPC between services, GraphQL only when client-driven field selection is worth a query planner and a cost limiter.

## In the interview

Bring networking in when you draw the first arrow: "clients resolve the gateway through GeoDNS with a 60-second TTL, terminate TLS 1.3 at the load balancer, and talk HTTP/2 to it; inside the datacenter services use gRPC over pooled connections." One sentence places DNS, TLS, HTTP and pooling without a separate lecture, and later questions about latency have a foundation to stand on.

Phrases that signal depth: "that is one more round trip, about 150 ms cross-region"; "HTTP/2 removes head-of-line blocking at the HTTP layer but not at TCP, which is what QUIC fixes"; "0-RTT only for idempotent requests because of replay".

??? question "Why does your chat design use WebSocket instead of SSE?"
    Because clients send as often as they receive, and one full-duplex connection is cheaper than an SSE stream down plus a POST per message up. If the system were notifications only, SSE would win on simplicity and proxy friendliness.

??? question "A WebSocket server holds a user's connection. How does a message from another server reach it?"
    A session registry maps user to node; the sending node looks up the recipient's node and publishes to it over a pub/sub channel (one topic per node or per user shard). If the recipient is offline, the message waits in storage and a push notification goes out instead.

??? question "How does a short DNS TTL help a regional failover, and what does it cost?"
    Resolvers re-ask after the TTL, so a 60-second TTL moves most traffic within a minute or two of changing the record. It costs more lookups against your authoritative servers and still leaves clients that ignore TTLs, which is why a health-checked load balancer or anycast in front is the faster failover.

??? question "What breaks if you expose gRPC directly to browsers?"
    Browsers cannot control HTTP/2 framing or trailers, so plain gRPC does not work; you need gRPC-Web through a proxy or a REST/JSON gateway that translates. Most designs keep gRPC internal and put REST or GraphQL at the edge.

??? question "Why not open a new database connection per request?"
    Each one costs a TCP and a TLS handshake and authentication, ~1 ms inside a datacenter plus CPU, and the database caps its connection count. A pool with timeouts amortises the setup and bounds the load; a connection proxy multiplexes thousands of app connections onto hundreds of database ones.

!!! tip "Interview tip"
    Count round trips out loud whenever you add a hop or a handshake: "that is TCP plus TLS 1.3, two round trips, about 1 ms in the datacenter and 300 ms cross-region". It converts protocol trivia into the latency budget the interviewer is actually grading.

## Common mistakes

- **WebSocket for everything**: a dashboard that updates every minute gets a stateful, proxy-hostile connection per viewer. Fix: poll or use SSE unless the client sends as often as it receives.
- **Ignoring the stateful tier**: drawing a WebSocket box with no registry, no pub/sub and no draining, so a deploy disconnects every user and messages cross nodes by luck. Fix: registry, pub/sub routing, heartbeats, graceful drain.
- **Treating HTTP/2 as the end of head-of-line blocking**: it removes it at the HTTP layer; one lost TCP packet still stalls every stream. Fix: name QUIC and HTTP/3 for lossy networks.
- **A long DNS TTL on a record you may need to move**: a day-long TTL turns a regional failover into a day of partial outage. Fix: short TTLs on records that steer traffic, anycast or a load balancer for fast failover.
- **Unbounded connections**: no pool limits, no timeouts, so one slow dependency holds every thread. Fix: pools with caps, connect and read timeouts, a connection proxy in front of the database.

!!! warning "Common mistake"
    Proposing 0-RTT resumption for every request to shave a round trip. Early data can be replayed by anyone on the path, so a 0-RTT `POST /payments` can be charged twice; only idempotent requests may ride in the first flight, and a candidate who knows that shows they understand what the round trip was buying.

## Self-check

??? question "How many round trips does a fresh HTTPS request cost with TLS 1.2, TLS 1.3 and QUIC, and what is that cross-region?"
    TCP plus two TLS round trips for 1.2 (three, ~450 ms at 150 ms each), TCP plus one for 1.3 (two, ~300 ms), one for QUIC (~150 ms); 0-RTT resumption puts the request in the first flight.

??? question "What does GeoDNS get wrong, and what fixes it?"
    It sees the resolver's location, not the user's, so a user on a distant public resolver is sent to the wrong region. Anycast routes by network topology, and client-side latency measurements can pick the region directly.

??? question "When is short polling the right realtime transport?"
    When updates arrive every minute or slower, or when the client and proxies cannot hold connections: it is stateless and cacheable, and its only cost is empty responses, which you size like any other QPS.

??? question "What must a design add when it introduces WebSocket?"
    A session registry of which node holds each connection, pub/sub routing between nodes, heartbeats to detect dead clients, a fallback to long polling, and a deploy process that drains connections.

??? question "Which protobuf changes are safe, and which break consumers?"
    Safe: adding fields with new numbers and defaults, deprecating fields without reusing their numbers. Breaking: renumbering or reusing a field, changing a field's type, making a field required.

## Related

- [API design for HLD rounds](api-design.md) — REST conventions, pagination, idempotency, gRPC and GraphQL trade-offs
- [Load balancing, reverse proxies and API gateways](load-balancing-and-api-gateway.md) — where TLS terminates and how L4 and L7 differ
- [Design a chat system](../case-studies/chat-messenger.md) — the WebSocket tier built for 50M DAU
- [Security essentials](security-essentials.md) — certificates, mutual TLS and token transport
- IETF RFC 8446, "The Transport Layer Security (TLS) Protocol Version 1.3" (2018)
- IETF RFC 9000, "QUIC: A UDP-Based Multiplexed and Secure Transport" (2021)
- IETF RFC 9113, "HTTP/2" (2022)
