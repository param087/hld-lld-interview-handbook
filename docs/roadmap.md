---
title: Study roadmap
description: An 8-week plan and a 1-week crash plan that order every page of the handbook for an SDE2 candidate.
---
# Study roadmap

Two ways through the handbook. The **8-week plan** (10–12 hours a week) covers everything, P0 pages first. The **1-week crash plan** (6–8 hours a day) is for an interview next week: only P0 material and the mocks.

Each week ends with a deliverable — something you write or run without looking at the page. That is the difference between having read a design and being able to produce it in 45 minutes.

## 8-week plan

| Week | Theme | Focus |
|---|---|---|
| 1 | [Foundations](#week-1-foundations) | 6 HLD fundamentals, 1 case study, 6 LLD fundamentals/patterns, 1 LLD problem |
| 2 | [Data layer](#week-2-data-layer) | 5 HLD fundamentals, 3 case studies, 6 LLD fundamentals/patterns, 3 LLD problems |
| 3 | [Correctness at scale](#week-3-correctness-at-scale) | 3 HLD fundamentals, 3 case studies, 8 LLD fundamentals/patterns, 3 LLD problems, 1 mock |
| 4 | [Coordination and real-time](#week-4-coordination-and-real-time) | 4 HLD fundamentals, 4 case studies, 5 LLD fundamentals/patterns, 4 LLD problems, 1 mock |
| 5 | [Architecture and money](#week-5-architecture-and-money) | 3 HLD fundamentals, 4 case studies, 5 LLD fundamentals/patterns, 4 LLD problems, 1 mock |
| 6 | [Pipelines and analytics](#week-6-pipelines-and-analytics) | 4 HLD fundamentals, 6 case studies, 4 LLD fundamentals/patterns, 5 LLD problems, 1 mock |
| 7 | [Breadth](#week-7-breadth) | 2 HLD fundamentals, 6 case studies, 4 LLD fundamentals/patterns, 7 LLD problems, 2 mocks |
| 8 | [Polish](#week-8-polish) | 4 case studies, 9 LLD problems |

### Week 1: Foundations

- **HLD fundamentals:** [The 45-minute HLD framework](hld/fundamentals/interview-framework.md), [Back-of-envelope estimation](hld/fundamentals/estimation.md), [From one server to millions of users](hld/fundamentals/scaling-primer.md), [Networking for system design](hld/fundamentals/networking-essentials.md), [API design for HLD rounds](hld/fundamentals/api-design.md), [Load balancing, reverse proxies and API gateways](hld/fundamentals/load-balancing-and-api-gateway.md)
- **Case studies:** [Design a URL shortener](hld/case-studies/url-shortener.md)
- **LLD fundamentals and patterns:** [Object-oriented Python for interviews](lld/fundamentals/oop-in-python.md), [The LLD interview framework](lld/fundamentals/lld-interview-framework.md), [Design patterns overview](lld/patterns/patterns-overview.md), [Strategy](lld/patterns/strategy.md), [Factory Method](lld/patterns/factory-method.md), [Singleton](lld/patterns/singleton.md)
- **LLD problems:** [Design a parking lot](lld/problems/parking-lot.md)
- **Deliverable:** Write the 45-minute HLD framework from memory; run the parking-lot demo and read its tests.

### Week 2: Data layer

- **HLD fundamentals:** [Caching and CDNs](hld/fundamentals/caching-and-cdn.md), [Choosing a database](hld/fundamentals/databases-sql-vs-nosql.md), [Storage engines and indexing](hld/fundamentals/storage-engines-and-indexing.md), [Replication](hld/fundamentals/replication.md), [Partitioning, sharding and consistent hashing](hld/fundamentals/partitioning-and-consistent-hashing.md)
- **Case studies:** [Design a distributed rate limiter](hld/case-studies/rate-limiter.md), [Design a Dynamo-style key-value store](hld/case-studies/key-value-store.md), [Design a distributed unique ID generator](hld/case-studies/unique-id-generator.md)
- **LLD fundamentals and patterns:** [SOLID in Python](lld/fundamentals/solid-principles.md), [UML with Mermaid](lld/fundamentals/uml-with-mermaid.md), [State](lld/patterns/state.md), [Observer](lld/patterns/observer.md), [Builder](lld/patterns/builder.md), [Decorator](lld/patterns/decorator.md)
- **LLD problems:** [Design a vending machine (and a coffee machine)](lld/problems/vending-machine.md), [Design tic-tac-toe (an extensible board game)](lld/problems/tic-tac-toe.md), [Design an in-memory cache (LRU, LFU, TTL)](lld/problems/in-memory-cache.md)
- **Deliverable:** Produce an estimation sheet for three systems; implement an LRU cache from scratch without looking.

### Week 3: Correctness at scale

- **HLD fundamentals:** [Transactions, 2PC, sagas and idempotency](hld/fundamentals/transactions-and-distributed-transactions.md), [CAP, PACELC and consistency models](hld/fundamentals/cap-pacelc-and-consistency-models.md), [Messaging, queues and Kafka internals](hld/fundamentals/messaging-and-event-streaming.md)
- **Case studies:** [Design a news feed](hld/case-studies/news-feed.md), [Design a chat system](hld/case-studies/chat-messenger.md), [Design a notification system](hld/case-studies/notification-system.md)
- **LLD fundamentals and patterns:** [DRY, KISS, YAGNI, Demeter, GRASP and cohesion](lld/fundamentals/design-principles-beyond-solid.md), [Concurrency for LLD in Python](lld/fundamentals/concurrency-for-lld.md), [Command](lld/patterns/command.md), [Template Method](lld/patterns/template-method.md), [Facade](lld/patterns/facade.md), [Adapter](lld/patterns/adapter.md), [Dependency Injection](lld/patterns/dependency-injection.md), [Repository](lld/patterns/repository.md)
- **LLD problems:** [Design an elevator system](lld/problems/elevator-system.md), [Design Splitwise](lld/problems/splitwise.md), [Design a rate limiter (LLD)](lld/problems/rate-limiter-lld.md)
- **Mock interview:** [Mock HLD interview: news feed](mocks/mock-hld-news-feed.md)
- **Deliverable:** Redo the news-feed mock timed; write a concurrency test for the elevator controller.

### Week 4: Coordination and real-time

- **HLD fundamentals:** [Consensus and coordination](hld/fundamentals/consensus-and-coordination.md), [Time, clocks and ordering](hld/fundamentals/time-and-ordering.md), [Rate limiting](hld/fundamentals/rate-limiting.md), [Resilience patterns](hld/fundamentals/resilience-patterns.md)
- **Case studies:** [Design Uber (with a DoorDash variant)](hld/case-studies/ride-sharing.md), [Design Yelp (proximity service)](hld/case-studies/proximity-service.md), [Design Nearby Friends](hld/case-studies/nearby-friends.md), [Design typeahead autocomplete](hld/case-studies/typeahead.md)
- **LLD fundamentals and patterns:** [Chain of Responsibility](lld/patterns/chain-of-responsibility.md), [Mediator](lld/patterns/mediator.md), [Composite](lld/patterns/composite.md), [Iterator](lld/patterns/iterator.md), [Proxy](lld/patterns/proxy.md)
- **LLD problems:** [Design a movie ticket booking system (BookMyShow)](lld/problems/movie-ticket-booking.md), [Design a library management system](lld/problems/library-management.md), [Design a logging framework](lld/problems/logging-framework.md), [Design an in-memory pub/sub message queue](lld/problems/pub-sub-system.md)
- **Mock interview:** [Mock HLD interview: chat system](mocks/mock-hld-chat.md)
- **Deliverable:** Redo the chat mock timed; implement a seat hold with TTL and version check.

### Week 5: Architecture and money

- **HLD fundamentals:** [Monolith, microservices, CQRS and event sourcing](hld/fundamentals/microservices-and-architecture-styles.md), [Object, file, search, time-series and graph storage](hld/fundamentals/storage-systems-zoo.md), [Security essentials](hld/fundamentals/security-essentials.md)
- **Case studies:** [Design YouTube or Netflix](hld/case-studies/video-streaming.md), [Design Dropbox or Google Drive](hld/case-studies/cloud-file-storage.md), [Design Ticketmaster (with a hotel-booking variant)](hld/case-studies/ticketing-and-reservations.md), [Design a payment system and digital wallet](hld/case-studies/payment-system.md)
- **LLD fundamentals and patterns:** [Memento](lld/patterns/memento.md), [Unit of Work](lld/patterns/unit-of-work.md), [Null Object](lld/patterns/null-object.md), [Event Bus](lld/patterns/event-bus.md), [Pipeline and Middleware](lld/patterns/pipeline-middleware.md)
- **LLD problems:** [Design an ATM](lld/problems/atm.md), [Design a hotel management system](lld/problems/hotel-management.md), [Design a text editor with undo and redo](lld/problems/text-editor.md), [Design an in-memory key-value store with transactions](lld/problems/kv-store-transactions.md)
- **Mock interview:** [Mock HLD interview: Ticketmaster](mocks/mock-hld-ticketmaster.md)
- **Deliverable:** Redo the Ticketmaster mock timed; write a double-entry ledger with idempotency keys.

### Week 6: Pipelines and analytics

- **HLD fundamentals:** [Observability, SLOs and error budgets](hld/fundamentals/observability-and-slos.md), [Geospatial indexing](hld/fundamentals/geospatial-indexing.md), [Batch and stream processing](hld/fundamentals/batch-and-stream-processing.md), [Probabilistic data structures](hld/fundamentals/probabilistic-data-structures.md)
- **Case studies:** [Design a distributed message queue](hld/case-studies/distributed-message-queue.md), [Design a metrics monitoring and alerting system](hld/case-studies/metrics-monitoring.md), [Design an ad click aggregation system](hld/case-studies/ad-click-aggregation.md), [Design a Top-K heavy hitters service](hld/case-studies/top-k-heavy-hitters.md), [Design a real-time gaming leaderboard](hld/case-studies/leaderboard.md), [Design a distributed cache](hld/case-studies/distributed-cache.md)
- **LLD fundamentals and patterns:** [Abstract Factory](lld/patterns/abstract-factory.md), [Specification](lld/patterns/specification.md), [Object Pool](lld/patterns/object-pool.md), [Visitor](lld/patterns/visitor.md)
- **LLD problems:** [Design a task scheduler (cron, LLD)](lld/problems/task-scheduler.md), [Design a stock brokerage system](lld/problems/stock-brokerage.md), [Design a food delivery system (Swiggy, Zomato, DoorDash)](lld/problems/food-delivery.md), [Design Uber (LLD) with driver matching](lld/problems/ride-sharing-lld.md), [Design a meeting scheduler and calendar](lld/problems/meeting-scheduler.md)
- **Mock interview:** [Mock LLD interview: elevator system](mocks/mock-lld-elevator.md)
- **Deliverable:** Redo the elevator mock timed; implement a windowed aggregator with a watermark.

### Week 7: Breadth

- **HLD fundamentals:** [Classic papers digest](hld/fundamentals/classic-papers-digest.md), [Deployments, feature flags and data migrations](hld/fundamentals/deployment-and-data-migrations.md)
- **Case studies:** [Design S3 (with a GFS/HDFS variant)](hld/case-studies/object-storage.md), [Design a distributed job scheduler](hld/case-studies/job-scheduler.md), [Design Google Docs](hld/case-studies/collaborative-editor.md), [Design Amazon (e-commerce with inventory and flash sales)](hld/case-studies/ecommerce-platform.md), [Design a search engine (with Twitter real-time search)](hld/case-studies/search-engine.md), [Design Twitch (live streaming with live comments)](hld/case-studies/live-streaming-and-comments.md)
- **LLD fundamentals and patterns:** [Prototype](lld/patterns/prototype.md), [Bridge](lld/patterns/bridge.md), [Flyweight](lld/patterns/flyweight.md), [Interpreter](lld/patterns/interpreter.md)
- **LLD problems:** [Design chess](lld/problems/chess.md), [Design snake and ladder](lld/problems/snake-and-ladder.md), [Design Stack Overflow](lld/problems/stack-overflow.md), [Design Amazon (cart, order, inventory, payment)](lld/problems/ecommerce-order-inventory.md), [Design Cricinfo (live scoreboard)](lld/problems/cricinfo.md), [Design a traffic signal controller](lld/problems/traffic-signal.md), [Design an in-memory file system](lld/problems/in-memory-file-system.md)
- **Mock interview:** [Mock LLD interview: parking lot](mocks/mock-lld-parking-lot.md), [Mock LLD interview: movie ticket booking](mocks/mock-lld-movie-ticket-booking.md)
- **Deliverable:** Redo the parking-lot and movie-booking mocks timed.

### Week 8: Polish

- **Case studies:** [Design a web crawler](hld/case-studies/web-crawler.md), [Design Gmail](hld/case-studies/email-service.md), [Design a stock exchange](hld/case-studies/stock-exchange.md), [Design LeetCode (online judge)](hld/case-studies/online-judge.md)
- **LLD problems:** [Design LinkedIn (social network)](lld/problems/linkedin.md), [Design a payment gateway and digital wallet](lld/problems/payment-gateway-wallet.md), [Design a notification service (LLD)](lld/problems/notification-service.md), [Design an online auction](lld/problems/online-auction.md), [Design a restaurant management system](lld/problems/restaurant-management.md), [Design a learning management system](lld/problems/learning-management.md), [Design the snake game](lld/problems/snake-game.md), [Design a car rental system](lld/problems/car-rental.md), [Design a bowling alley](lld/problems/bowling-alley.md)
- **Cheatsheets:** all of them — see the [cheatsheets index](cheatsheets/index.md)
- **Deliverable:** Re-read every P0 page and all cheatsheets; two full timed mocks per track with a peer; review the common-mistakes sheet.

## 1-week crash plan

| Day | Focus | Pages |
|---|---|---|
| Day 1 | HLD core | [The 45-minute HLD framework](hld/fundamentals/interview-framework.md), [Back-of-envelope estimation](hld/fundamentals/estimation.md), [From one server to millions of users](hld/fundamentals/scaling-primer.md), [Caching and CDNs](hld/fundamentals/caching-and-cdn.md), [Choosing a database](hld/fundamentals/databases-sql-vs-nosql.md), [Partitioning, sharding and consistent hashing](hld/fundamentals/partitioning-and-consistent-hashing.md), [Latency numbers and estimation tables](cheatsheets/latency-and-estimation.md), [Database selection matrix](cheatsheets/database-selection-matrix.md), [HLD round checklist](cheatsheets/hld-checklist.md) |
| Day 2 | HLD correctness + P0 case studies | [Replication](hld/fundamentals/replication.md), [Transactions, 2PC, sagas and idempotency](hld/fundamentals/transactions-and-distributed-transactions.md), [CAP, PACELC and consistency models](hld/fundamentals/cap-pacelc-and-consistency-models.md), [Messaging, queues and Kafka internals](hld/fundamentals/messaging-and-event-streaming.md), [Rate limiting](hld/fundamentals/rate-limiting.md), [Resilience patterns](hld/fundamentals/resilience-patterns.md), [Design a URL shortener](hld/case-studies/url-shortener.md), [Design a distributed rate limiter](hld/case-studies/rate-limiter.md), [Design a Dynamo-style key-value store](hld/case-studies/key-value-store.md), [Design a distributed unique ID generator](hld/case-studies/unique-id-generator.md) |
| Day 3 | LLD core | [Object-oriented Python for interviews](lld/fundamentals/oop-in-python.md), [SOLID in Python](lld/fundamentals/solid-principles.md), [The LLD interview framework](lld/fundamentals/lld-interview-framework.md), [Concurrency for LLD in Python](lld/fundamentals/concurrency-for-lld.md), [Strategy](lld/patterns/strategy.md), [State](lld/patterns/state.md), [Observer](lld/patterns/observer.md), [Factory Method](lld/patterns/factory-method.md), [Singleton](lld/patterns/singleton.md), [Command](lld/patterns/command.md), [Builder](lld/patterns/builder.md), [Decorator](lld/patterns/decorator.md), [Design a parking lot](lld/problems/parking-lot.md), [Design a vending machine (and a coffee machine)](lld/problems/vending-machine.md) |
| Day 4 | P0 case studies | [Design a news feed](hld/case-studies/news-feed.md), [Design a chat system](hld/case-studies/chat-messenger.md), [Design YouTube or Netflix](hld/case-studies/video-streaming.md), [Design Uber (with a DoorDash variant)](hld/case-studies/ride-sharing.md), [Design Ticketmaster (with a hotel-booking variant)](hld/case-studies/ticketing-and-reservations.md), [Networking for system design](hld/fundamentals/networking-essentials.md), [Geospatial indexing](hld/fundamentals/geospatial-indexing.md) |
| Day 5 | P0 problems | [Design an elevator system](lld/problems/elevator-system.md), [Design Splitwise](lld/problems/splitwise.md), [Design a movie ticket booking system (BookMyShow)](lld/problems/movie-ticket-booking.md), [Design an in-memory cache (LRU, LFU, TTL)](lld/problems/in-memory-cache.md), [Design tic-tac-toe (an extensible board game)](lld/problems/tic-tac-toe.md), [Design a rate limiter (LLD)](lld/problems/rate-limiter-lld.md), [Problem to pattern quick reference](cheatsheets/pattern-quick-reference.md), [LLD round checklist](cheatsheets/lld-checklist.md) |
| Day 6 | Mocks (read, then redo timed from the prompt alone) | [Mock HLD interview: news feed](mocks/mock-hld-news-feed.md), [Mock HLD interview: Ticketmaster](mocks/mock-hld-ticketmaster.md), [Mock LLD interview: parking lot](mocks/mock-lld-parking-lot.md), [Mock LLD interview: movie ticket booking](mocks/mock-lld-movie-ticket-booking.md) |
| Day 7 | Review | [Common SDE2 mistakes in design rounds](cheatsheets/common-mistakes-sde2.md), [Clarifying-question bank](cheatsheets/questions-to-ask.md), [Consistency, replication and isolation tables](cheatsheets/consistency-and-replication-tradeoffs.md), [Queue and stream selection](cheatsheets/messaging-selection.md), [Glossary](cheatsheets/glossary.md) |

On day 7, also redraw five architectures from memory (news feed, chat, Ticketmaster, key-value store, Uber) and compare them with the pages.

## How to study a page

1. Read the TL;DR and the diagrams first; try to predict the deep dives before reading them.
2. For case studies, redo the estimation on paper. For LLD problems, write the class diagram before looking at the implementation, then run the tests.
3. Answer the follow-up questions out loud. If you cannot, that is the section to reread tomorrow.
4. Log the page in your own one-line-per-page notebook: the crux, the numbers, the pattern.

## Related

- [The 45-minute HLD framework](hld/fundamentals/interview-framework.md)
- [The LLD interview framework](lld/fundamentals/lld-interview-framework.md)
- [HLD round checklist](cheatsheets/hld-checklist.md) and [LLD round checklist](cheatsheets/lld-checklist.md)
- [Common SDE2 mistakes in design rounds](cheatsheets/common-mistakes-sde2.md)
