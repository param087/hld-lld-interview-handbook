---
title: Design a chat system
description: WhatsApp/Messenger/Slack-style messaging — WebSocket session registry, per-conversation sequencing, wide-column storage with multi-device sync, delivery states, group fan-out and presence, with a tested Python router.
---
# Design a chat system

## TL;DR

- A chat system is a **stateful real-time problem**: millions of long-lived WebSockets, each pinned to one server, and a write path that must find the right server for every recipient in under a round trip.
- The cruxes an interviewer probes: (1) **connection management and the session registry** (which server holds a user), (2) **per-conversation ordering** with sequence numbers and idempotent ids, (3) **storage by conversation** plus multi-device sync, (4) **delivery states** with push for offline users, (5) **group fan-out and presence** without melting the pub/sub tier.
- It handles 50M DAU and 70k messages/s at peak with ~150 chat servers, a Redis session registry, a per-server pub/sub channel, and a wide-column message store partitioned by `conversation_id`.

## Problem statement and clarifying questions

"Design a messaging service like WhatsApp: one-to-one and group chats, delivered in real time when the recipient is online and on reconnect when not, with sent/delivered/read indicators." Group sizes and multi-device decide how much fan-out you sign up for.

| Question | Assumption taken |
|---|---|
| One-to-one only, or groups too? | Both; groups capped at 500. Million-reader broadcast channels are a follow-up. |
| Scale? | 50M DAU, 40 messages sent per user per day, 20% of DAU online at peak. |
| Multi-device? | Yes: up to 5 devices per user, all converging on the same history. |
| Delivery guarantees? | Never lost once the sender sees "sent"; duplicates invisible to users. |
| Ordering? | Total order within a conversation; no promise across conversations. |
| Latency target? | Online recipient sees the message in < 500 ms p99 end to end. |
| Message size and media? | Text up to 4 KB; images and files via object storage, referenced by id. |
| End-to-end encryption? | Yes for bodies; the server routes ciphertext and still tracks delivery state. |
| Retention? | Server keeps history for sync; the client is the primary store (WhatsApp model). |

## Requirements

### Functional

- Send a text message to a one-to-one or group conversation; attach media by id.
- Deliver in real time to every online device of every member, including the sender's other devices.
- Deliver on reconnect to devices that were offline; notify offline users through push.
- Show per-message state to the sender: sent, delivered, read (group: when all members reach it).
- Presence: online/offline and "last seen" for contacts; typing indicators.

### Non-functional

- Scale: 2B messages/day — ~23k/s average, ~70k/s peak — over 10M concurrent connections.
- Latency: < 500 ms p99 tap to screen when both are online; < 200 ms p99 for the sender's "sent" ack.
- Consistency: strong ordering inside a conversation (one sequencer per conversation); eventual elsewhere.
- Durability: a message acknowledged as "sent" is replicated to 3 nodes before the ack.
- Availability: 99.99% for send and receive; a server crash costs its users one reconnect, not messages.

### Out of scope

Voice and video calls, spam and abuse detection, search over history, the key-exchange protocol itself, media processing.

## Estimation

Using the [latency and estimation tables](../../cheatsheets/latency-and-estimation.md) (a day is ~10^5 s, peak is 3x average):

| Quantity | Arithmetic | Result |
|---|---|---|
| Messages per day | 50M DAU x 40 | 2B/day |
| Write QPS | 2B / 86,400 | ~23k/s average, ~70k/s peak |
| Socket writes (deliveries) | ~2 sockets per message (recipient plus the sender's other device) | ~50k/s average, ~150k/s peak |
| Read QPS (history and sync) | 50M x 10 conversation opens/day / 10^5 | ~5k/s: the read path is small, the push path is the problem |
| Storage | 2B x 100 B = 200 GB/day x 365 | ~73 TB/year, ~220 TB/year with 3 replicas |
| Media | 5% of messages x 200 KB | 20 TB/day into object storage, served by CDN |
| Bandwidth | 70k/s x 1 KB frame in, x2 out | ~70 MB/s in, ~140 MB/s out: ~1 Gbps, never the bottleneck |
| Concurrent sockets | 50M x 20% online at peak | 10M long-lived connections |
| Chat servers | 100k sockets per server (an assumption): 10M / 100k, x1.5 headroom | ~150 servers |
| Session registry | 10M sessions x ~100 B; 70k sends x ~3 lookups | ~1 GB in Redis, ~200k lookups/s: 4 shards at ~100k ops/s |
| Message store | 70k writes/s x 3 replicas = 210k node-writes/s at ~5k-10k per node | ~25-40 wide-column nodes |
| Recent-messages cache | 20% of 50M conversations x 50 messages x 100 B | ~50 GB |

Say two things out loud: the read/write ratio is ~**1:1**, so there is no feed to precompute; and the hard resource is **10M open sockets**, forcing a stateful tier and a registry mapping users to servers.

## API design

Messaging rides on one WebSocket per device; REST covers everything that is not latency-critical.

| Endpoint or frame | Request | Response | Notes |
|---|---|---|---|
| `WS /v1/connect` | upgrade with bearer token, `device_id`, `last_synced` map | `connected {server_id, resume_token}` | The server registers the session; the frames below flow over this socket. |
| frame `send` | `{conversation_id, client_msg_id, body, media_id?}` | `ack {client_msg_id, seq, sent_at}` | `client_msg_id` is the idempotency key: a retry returns the same `seq`. |
| frame `delivered` / `read` | `{conversation_id, up_to_seq}` | none | Cursors, not per-message: one idempotent frame acks everything up to `seq`. |
| `GET /v1/conversations/{id}/messages?after_seq=100&limit=100` | — | `200 {messages[], has_more}` | Sync and scroll-back paginate by `seq` (`before_seq` for history); no offsets. |
| `POST /v1/conversations` | `{kind, member_ids[]}` + `Idempotency-Key` | `201 {conversation_id}` | Direct chats are deduplicated on the sorted member pair. |
| `POST /v1/devices` | `{device_id, push_token, platform}` | `204` | Push tokens live with the device, not the user. |
| `POST /v1/media/uploads` | `{content_type, size}` | `200 {upload_url, media_id}` | Presigned upload straight to object storage. |

The user id always comes from the token. Every server-to-client frame carries `seq`, so a client that holds 41 and receives 43 syncs for 42 instead of trusting the socket.

## Data model

**Messages are partitioned by conversation and clustered by sequence number; sessions are a cache, not a table.**

```mermaid
erDiagram
    USER ||--o{ DEVICE : owns
    USER ||--o{ MEMBERSHIP : joins
    CONVERSATION ||--o{ MEMBERSHIP : has
    CONVERSATION ||--o{ MESSAGE : orders
    USER ||--o{ MESSAGE : sends
    MESSAGE ||--o{ RECEIPT : tracked_by
    DEVICE ||--o| SESSION : holds
    USER {
        bigint user_id PK
        string handle
        timestamp last_seen
    }
    DEVICE {
        bigint device_id PK
        bigint user_id
        string push_token
        json last_synced_seq "conversation_id to seq"
    }
    CONVERSATION {
        bigint conversation_id PK
        string kind "direct or group"
        timestamp created_at
    }
    MEMBERSHIP {
        bigint conversation_id PK
        bigint user_id PK
        bigint read_up_to_seq
        timestamp joined_at
    }
    MESSAGE {
        bigint conversation_id PK "partition key"
        bigint seq PK "clustering key, descending"
        bigint sender_id
        string client_msg_id "idempotency key"
        string body "ciphertext under E2EE"
        timestamp sent_at
    }
    RECEIPT {
        bigint conversation_id PK
        bigint seq PK
        bigint recipient_id PK
        string state "sent, delivered, read"
        timestamp updated_at
    }
    SESSION {
        bigint user_id PK
        bigint device_id PK
        string server_id
        timestamp connected_at
    }
```

Store choices, with the sentence to say for each:

- **Messages**: a wide-column store (Cassandra, ScyllaDB, HBase), partition key `conversation_id`, clustering key `seq` descending — every product query ("latest 50", "after seq N", "before seq N") is one partition scan. Bucket long conversations as `(conversation_id, seq // 10_000)` so no partition grows without bound.
- **Receipts**: same store, keyed by `(conversation_id, seq, recipient_id)`; groups store a counter of delivered/read members instead of a row per recipient.
- **Membership and conversations**: a key-value store keyed by `conversation_id`, plus a reverse index `user_id -> conversation_ids`.
- **Sessions**: Redis hash per user (`sessions:{user_id}`) and a liveness key per server — rebuildable, since clients reconnect and rewrite it.
- **Media**: object storage behind a CDN; the message row stores `media_id` and the ciphertext key.

## High-level design

**v1: sticky WebSockets on a stateful tier, a chat service that sequences and routes, and a registry plus pub/sub to cross servers.**

```mermaid
flowchart LR
    subgraph clients["Clients"]
        c_phone["Phone app"]
        c_web["Web / desktop app"]
    end
    subgraph edge["Edge"]
        e_l4["L4 load balancer<br/>(long-lived WebSocket)"]
        e_gw["API gateway<br/>(REST: history, media, devices)"]
    end
    subgraph services["Services"]
        s_ws["Chat servers (WebSocket)"]
        s_chat["Chat service:<br/>sequencer + router"]
        s_presence["Presence service"]
        s_push["Push service"]
    end
    subgraph async["Async"]
        a_kafka[["Kafka: message-stored"]]
        a_workers["Offline / push workers"]
    end
    subgraph data["Data"]
        d_msgs[("Message store<br/>(wide-column)")]
        d_registry[("Session registry (Redis)")]
        d_pubsub[("Redis pub/sub:<br/>one channel per server")]
        d_conv[("Conversations + membership")]
        d_media[("Object storage + CDN")]
    end
    c_phone --> e_l4 --> s_ws
    c_web --> e_l4
    c_phone --> e_gw
    e_gw --> s_chat
    s_ws --> s_chat
    s_chat --> d_msgs
    s_chat --> d_conv
    s_chat --> d_registry
    s_chat --> d_pubsub --> s_ws
    s_chat --> a_kafka --> a_workers --> s_push
    s_ws --> s_presence --> d_registry
    e_gw --> d_media
```

**Write path: sequence, persist, ack the sender, then find the recipient's server and publish to it.**

```mermaid
sequenceDiagram
    autonumber
    actor A as Ann (phone)
    participant W1 as Chat server ws-1
    participant CS as Chat service
    participant DB as Message store
    participant R as Session registry
    participant PS as Pub/sub
    participant W2 as Chat server ws-2
    actor B as Bob (phone)
    A->>W1: send {conversation_id, client_msg_id, body}
    W1->>CS: route(sender session, message)
    CS->>CS: dedup on (sender, client_msg_id), assign seq
    CS->>DB: write row (conversation_id, seq)
    DB-->>CS: ok (3 replicas)
    CS-->>W1: ack {seq}
    W1-->>A: sent (one tick)
    CS->>R: sessions(bob)
    R-->>CS: bob-phone is on ws-2
    CS-)PS: publish to channel ws-2
    PS-)W2: envelope {bob-phone, message}
    W2->>B: message frame {seq, body}
    B-->>W2: delivered {up_to_seq}
    W2->>CS: receipt(bob, seq, delivered)
    CS->>DB: update receipt
    CS-)PS: publish to channel ws-1
    PS-)W1: receipt
    W1-->>A: delivered (two ticks)
```

**Read path: a device reconnects, re-registers, and pulls everything after the last seq it holds.**

```mermaid
sequenceDiagram
    autonumber
    actor C as Cat (phone, was offline)
    participant LB as L4 load balancer
    participant W1 as Chat server ws-1
    participant R as Session registry
    participant CS as Chat service
    participant CH as Recent-messages cache
    participant DB as Message store
    C->>LB: WebSocket connect (token, device_id)
    LB->>W1: new TCP connection, stays pinned
    W1->>R: HSET sessions:cat cat-phone ws-1
    W1-->>C: connected
    C->>W1: sync {conversation_id: trio, after_seq: 0}
    W1->>CS: sync(cat, trio, after_seq=0)
    CS->>CH: latest rows of trio
    alt cache hit
        CH-->>CS: rows with seq > 0
    else miss
        CS->>DB: partition scan, seq > 0, limit 100
        DB-->>CS: rows
    end
    CS-->>W1: messages 1..n, has_more
    W1-->>C: batch frame
    C->>W1: delivered {up_to_seq: n}
    W1->>CS: receipts(cat, trio, delivered up to n)
```

Walk-through: the sender's ack depends on one durable write, so "sent" arrives in about one store round trip. Finding Bob costs one registry lookup and one publish; no chat server talks to another directly. An offline device needs no queue — the message store *is* the queue, indexed by `seq`, and push only wakes the device up.

## Deep dive: connection management and the session registry

The probing question is "Ann is connected to server 17 and Bob to server 203 — how does Ann's message reach Bob?" First the transport:

| Transport | Server push | Cost per idle client | Breaks when |
|---|---|---|---|
| Short polling | Emulated | 10M clients at 5 s = 2M QPS of mostly empty responses | Any real scale |
| Long polling | Yes, one message per request | A held request plus reconnect per message | High message rates; proxies time out |
| SSE | Yes, server to client only | One HTTP connection | Client-to-server needs a second channel |
| WebSocket | Yes, both directions, framed | One TCP connection, ~one heartbeat per 30 s | Stateful servers: you now need a registry |

WebSocket wins because chat traffic is bidirectional and bursty; the price is that a user is *somewhere* and every sender must find out where (background: the [networking page](../fundamentals/networking-essentials.md)).

The mechanism has three parts. An **L4 load balancer** pins a new connection to a server — one TCP stream, so stickiness is free. The server writes `HSET sessions:{user_id} {device_id} {server_id}` to the **registry** and deletes it on a clean close. Every server **subscribes to its own pub/sub channel**, so a router publishes to `channel:{server_id}` and that server writes the frame locally. Heartbeats run at two levels: ping/pong per socket every 30 s catches half-open TCP connections, and a per-server liveness key refreshed every 5 s makes crashes cheap — nobody deletes 100k hash fields; readers see a stale server id, treat the user as offline and clean up lazily.

```python title="code/hld/chat_router.py — registry, bus and chat server"
--8<-- "code/hld/chat_router.py:registry"
```

```python title="code/hld/chat_router.py — cross-server pub/sub"
--8<-- "code/hld/chat_router.py:bus"
```

Two details that distinguish a strong answer: the registry is per *device*, not per user, since multi-device means fan-out to every session; and it is a **rebuildable cache** — losing it costs one reconnect storm, absorbed with backoff and rate limits, not a replicated database.

## Deep dive: ordering, message ids and storage

"Bob's phone shows my messages in a different order than my laptop" is the bug this deep dive prevents. The options for ordering a conversation:

| Order by | Total order? | Gap detection | Cost | Fails when |
|---|---|---|---|---|
| Client timestamp | No: clocks skew and go backwards | None | Free | Two phones disagree by seconds |
| Server arrival time | Only per server | None | Free | Two servers sequence the same conversation |
| Global Snowflake id | Yes, roughly by time | None (ids are sparse) | An id service | Clients cannot tell "I missed one" |
| Per-conversation sequence number | Yes, exact | Yes: 41 then 43 means 42 is missing | A single sequencer per conversation | The sequencer is a hot spot for a giant channel |

Choose the **per-conversation `seq`** assigned by a single owner: the chat-service shard that owns `conversation_id` (a Redis `INCR` per conversation in v1). Dense ids make every API cursor an integer, and a device that holds 41 and receives 43 syncs 42 without server help. Keep a time-sortable global id as a secondary attribute for search and export, but never order by it — [time and ordering](../fundamentals/time-and-ordering.md) explains why wall clocks cannot do this job.

**Multi-device sync** falls out of the same key: each device keeps `last_synced_seq` per conversation and asks for `after_seq` on connect, so the server tracks nothing about which device has what — it answers range queries. Idempotency is part of the write: `(sender_id, client_msg_id)` maps to the `seq` it got, so a retry after a lost ack returns the same message instead of a duplicate.

```python title="code/hld/chat_router.py — sequencing, routing, receipts and sync"
--8<-- "code/hld/chat_router.py:service"
```

Note where the lock sits: sequencing and storing happen under it, routing outside it, so one slow socket never delays the next message's `seq`.

## Deep dive: delivery states, offline queue and push

The sender's ticks are a per-recipient state machine whose one rule is **forward only**: a late "delivered" ack arriving after a "read" cursor must not regress the state.

**Message status as the sender's client and the server see it.**

```mermaid
stateDiagram-v2
    [*] --> Pending : user taps send
    Pending --> Pending : retry with same client_msg_id
    Pending --> Sent : server ack with seq
    Pending --> Failed : no ack after N retries
    Failed --> Pending : user retries
    Sent --> Delivered : recipient device acks
    Delivered --> Read : recipient opens conversation
    Sent --> Read : read cursor arrives before delivered ack
    Read --> [*]
```

Delivered and read are **cursors, not per-message events**: the device sends `delivered {up_to_seq}` once per batch, and "read" is the conversation's `read_up_to_seq`. That collapses 2B potential receipt writes a day into a fraction of that, and makes the server logic a comparison (`seq <= up_to_seq`) rather than a per-message lookup. For groups the sender's state is the weakest state across members, stored as a counter per message rather than 500 rows.

Offline handling needs no queue: the store already holds every message by `seq`, so a returning device syncs. The only extra work is a **push notification** to wake it, sent by workers that consume the `message-stored` topic, check the registry, and call the [notification system](notification-system.md) for users with no live session. Collapse pushes per conversation ("3 new messages") and never include an encrypted body.

What the module prints for the trio scenario (Cat offline, Ann on two devices):

```text
trio:1 sent from ann-phone; retry returned seq 1 (idempotent)
ann-laptop outbox: ['lunch?'] (multi-device)
bob-phone outbox:  ['lunch?'] (via ws-2 channel)
push notifications: [('cat', 'trio:1')] | cat online: False
status: sent
after bob's delivered ack: sent (cat has not received it)
cat connects, sync(after_seq=0) -> [(1, 'lunch?')]
after cat's delivered ack: delivered
after both read: read
trio:2: the order inside a conversation is a counter, never a clock
ws-2 gone: bob's session dropped -> ('bob', 'trio:3') | bob online: False
```

## Deep dive: group fan-out and presence

"What changes for a group of 500?" The message is still written **once**; fan-out happens at delivery, and the unit of work is the *server*, not the socket.

**Group send: one durable write, one registry batch, one publish per server that holds members.**

```mermaid
sequenceDiagram
    autonumber
    actor A as Ann
    participant W1 as Chat server ws-1
    participant CS as Chat service
    participant DB as Message store
    participant R as Session registry
    participant PS as Pub/sub
    participant W2 as Chat server ws-2
    participant PUSH as Push workers
    A->>W1: send to group of 200
    W1->>CS: route
    CS->>DB: write once (group_id, seq)
    CS-->>A: ack {seq} via ws-1
    CS->>R: sessions of 200 members (pipelined)
    R-->>CS: 120 sockets on 40 servers, 80 users offline
    par one publish per server
        CS-)PS: channel ws-1 with 3 envelopes
        CS-)PS: channel ws-2 with 5 envelopes
    and offline members
        CS-)PUSH: 80 collapsed push notifications
    end
    PS-)W2: envelopes
    W2->>W2: write the frame to each local socket
    Note over CS,PS: receipts are counted per message, not fanned back to every member
```

Sizing the fan-out: 70k messages/s at peak, mostly one-to-one with groups averaging tens of online members, lands around 150k socket writes/s and a few tens of thousands of publishes per second once envelopes are grouped by server — inside one Redis pub/sub shard's ~100k ops/s, with room for a second keyed by server id. Capping group size at 500 bounds the per-message cost; beyond that you switch to **fan-out on read**, where the channel stores the message once and clients pull by `seq`.

**Presence** is the same problem with a worse ratio: one flip for a user with 300 contacts is 300 deliveries, and a phone in a lift flips several times a minute. Three tactics: derive online state from the registry rather than a separate heartbeat, **debounce** transitions with a 30 s grace period before publishing "offline", and fan out only to contacts with the app open. "Last seen" is written lazily on disconnect; [Nearby Friends](nearby-friends.md) scales this pattern to 30-second location updates.

## Scaling, bottlenecks and failure modes

**v2: regional stateful tiers, a sharded registry and pub/sub, a partitioned log as the sequencer's durability, and a sharded message store.**

```mermaid
flowchart LR
    subgraph clients["Clients"]
        c_app["Apps (reconnect with jittered backoff)"]
    end
    subgraph edge["Edge (per region)"]
        e_dns["GeoDNS / anycast"]
        e_l4["L4 load balancers"]
    end
    subgraph ws["Stateful tier"]
        w_fleet["Chat servers x 150<br/>(~100k sockets each)"]
        w_presence["Presence fan-out"]
    end
    subgraph core["Core services"]
        s_router["Routers (stateless, N replicas)"]
        s_seq["Sequencer shards:<br/>one owner per conversation_id"]
        s_push["Push workers"]
    end
    subgraph data["Data"]
        d_registry[("Registry: Redis cluster<br/>sharded by user_id")]
        d_pubsub[("Pub/sub shards:<br/>channel per server")]
        d_kafka[["Kafka: partitioned by conversation_id"]]
        d_msgs[("Message store: sharded by<br/>conversation_id, 3 replicas")]
        d_cache[("Recent-messages cache")]
        d_media[("Object storage + CDN")]
    end
    c_app --> e_dns --> e_l4 --> w_fleet
    w_fleet --> s_router --> s_seq --> d_kafka --> d_msgs
    s_router --> d_registry
    s_router --> d_pubsub --> w_fleet
    d_kafka --> s_push
    w_fleet --> w_presence --> d_pubsub
    s_router --> d_cache
    c_app --> d_media
```

What changes from v1: the sequencer becomes a sharded service owning conversations by consistent hashing, appending each message to a Kafka partition keyed by `conversation_id` with all replicas acknowledging, and acking the sender from the log — the store and push workers are consumers. One hop buys ordering and durability, and a dead shard recovers its counters from the tail of its partitions.

What breaks first, and what you do about it:

- **Reconnect storms**: a crash or a deploy drops 100k sockets at once; clients back off with jitter, the load balancer caps new connections per second, and the registry absorbs 100k `HSET`s over a minute, not a second.
- **Registry staleness**: a router publishes to a server that is gone, finds no subscriber, marks the user offline and pushes; the client reconnects and syncs. Nothing is lost because the store, not the socket, is the source of truth.
- **Hot conversations**: a 500-member group at 10 messages/s is 5k deliveries/s on one sequencer and one partition. Sequencers are cheap, so cap group size and group publishes per server; larger audiences use fan-out on read.
- **Hot partitions in the store**: bucketing by `seq // 10_000` bounds any single partition; time-bucketing is the alternative when messages are sparse.
- **Kafka lag**: messages are already acked from the replicated log, so lag delays push notifications and store writes, never delivery to online users.
- **Multi-region**: pin a conversation to the home region of its sequencer; members elsewhere pay one cross-region round trip (~70-150 ms) on send, inside the 500 ms budget. Replicate the store asynchronously for local reads and failover.
- **Degradation order**: typing indicators go first, then presence fan-out, then read receipts; never the message path.

## Trade-offs summary

| Decision | Chosen | Alternatives | Why |
|---|---|---|---|
| Transport | WebSocket with sticky L4 | Long polling, SSE, HTTP/2 push | Bidirectional, one socket per device, cheapest idle cost |
| Finding a user | Registry (Redis hash per user) + channel per server | Broadcast to all servers, hashing users to servers | One lookup per recipient; no rebalancing on scale-out |
| Ordering | Per-conversation `seq` from one owner | Client time, global Snowflake | Exact total order and gap detection; cheap cursors |
| Message store | Wide-column by `conversation_id`, clustered by `seq` | Relational sharded by user, document store | Every query is a partition range scan |
| Delivery receipts | Cursors (`up_to_seq`), forward-only | Per-message events | Far fewer writes; no state regressions |
| Offline delivery | Store is the queue + push wake-up | Separate per-user queue | One source of truth; sync is a range read |
| Group fan-out | Write once, publish per server, cap size | Write per member (inbox model) | Bounded cost; an inbox multiplies storage by members |
| Ack point | After replicated write (log or quorum) | After recipient delivery | "Sent" means durable, not seen; a recipient may be offline for days |

## Interviewer follow-ups

??? question "How does end-to-end encryption change the design?"
    Bodies become ciphertext, keys are per device — which is why the registry is per device — and the server keeps only the metadata it routes on: conversation id, seq, sender, receipts. Server-side search, link previews and moderation disappear; a key directory and key-change notifications appear.

??? question "Why not a global Snowflake id for every message?"
    Keep one as a secondary id, but do not order by it: ids from different sequencers interleave by wall clock, and sparse ids give the client no way to detect a missing message. A dense per-conversation counter gives ordering and gap detection for free.

??? question "A chat server dies with 100k sockets. Walk me through the next 60 seconds."
    Clients see a failed ping or TCP reset and reconnect with jittered backoff; the load balancer spreads them over the remaining servers; each reconnect rewrites its registry entry and syncs by `after_seq`. Routers hitting the stale id find no subscriber, mark those users offline and push.

??? question "How would you support a 100k-member channel?"
    Switch from fan-out on delivery to fan-out on read: store the message once, publish a tiny "new seq" signal to online members, and let clients fetch by `seq` on open. Receipts become aggregate counters or are disabled, as in Slack and Telegram channels.

??? question "Can you guarantee exactly-once delivery?"
    No system does; you get at-least-once plus idempotency. `client_msg_id` makes resends safe on the write side, `seq` makes the read side idempotent (a device drops a frame whose seq it holds). Say "effectively once" and explain both halves.

??? question "Typing indicators and presence generate more traffic than messages. How do you keep them cheap?"
    Typing indicators are fire-and-forget, sent at most every 3 s, only to members with the conversation open, and never stored. Presence transitions are debounced over 30 s and fanned out only to contacts who are online. Both are the first things you shed under pressure.

!!! tip "Interview tip"
    Lead with the state: "10M open sockets means users are pinned to servers, so the first component is a registry saying where a user is, and the second is a way to publish to that server." That is why chat differs from every stateless case study.

!!! warning "Common mistake"
    Ordering messages by client or server timestamp. Two devices, two servers and one clock-skew incident later, the conversation reads differently on each phone. Assign a per-conversation sequence number from a single owner and make every API cursor that number.

## 45-minute pacing

| Minutes | What to say and draw |
|---|---|
| 0–5 | Clarify: groups up to 500, multi-device, E2EE bodies, 50M DAU, < 500 ms delivery, never lose a "sent" message. |
| 5–9 | Estimation: 23k msg/s average, 70k/s peak, 10M sockets, ~150 servers, 200 GB/day — sockets, not QPS, drive the design. |
| 9–14 | API over one WebSocket (send, delivered, read cursors) and the data model: messages by `conversation_id`, clustered by `seq`. |
| 14–24 | v1 diagram; narrate the write path (seq, durable write, ack, registry lookup, publish) and the reconnect sync path. |
| 24–40 | Deep dives in order: registry and heartbeats, sequencing and idempotency, delivery cursors plus push, group fan-out and presence debouncing. |
| 40–45 | v2: sharded registry and pub/sub, log-backed sequencers, reconnect storms, degradation order; close on the trade-offs table. |

## Related

- [Networking for system design](../fundamentals/networking-essentials.md) — WebSocket vs long polling vs SSE, L4 vs L7, keepalives
- [Design Nearby Friends](nearby-friends.md) — the same registry and pub/sub pattern under 333k updates/s
- [Design a notification system](notification-system.md) — the push path for offline recipients
- [Mock HLD interview: chat system](../../mocks/mock-hld-chat.md) — this design as a 45-minute transcript
- [Messaging, queues and Kafka internals](../fundamentals/messaging-and-event-streaming.md) — partitioning and acks=all as the durability hop
- [Time, clocks and ordering](../fundamentals/time-and-ordering.md) — why a counter beats a timestamp
- Primary sources: RFC 6455 (The WebSocket Protocol); Discord Engineering, "How Discord Stores Trillions of Messages" (2023)
