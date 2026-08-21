---
title: Problem to pattern quick reference
description: Look up the symptom the interviewer just described, get the pattern, the Python idiom that usually replaces the textbook shape, and the problem page that uses it.
---
# Problem to pattern quick reference

## How to use this sheet

Read the left column, not the pattern names. In the room you hear a symptom ("we may add more fare rules"), and you owe one sentence: symptom, pattern, the follow-up it turns into a one-class change. The idiom column is what you would actually write in Python; the last column is a page with tested code.

## Tables

### Creational: something is built and the concrete class varies

| Symptom you hear | Pattern | Python idiom | Worked in |
|---|---|---|---|
| A type arrives as a string or code from outside | [Factory Method](../lld/patterns/factory-method.md) | dict registry, `classmethod` constructor | [Parking lot](../lld/problems/parking-lot.md) |
| Variants must be built as a matching set, never mixed | [Abstract Factory](../lld/patterns/abstract-factory.md) | frozen dataclass of callables | [Payment gateway](../lld/problems/payment-gateway-wallet.md) |
| Construction takes ten arguments, most optional | [Builder](../lld/patterns/builder.md) | keyword-only args, `dataclasses.replace` | [Meeting scheduler](../lld/problems/meeting-scheduler.md) |
| You need another copy of an already configured object | [Prototype](../lld/patterns/prototype.md) | `copy.deepcopy`, `replace` | [Chess](../lld/problems/chess.md) |
| "There is only one of these in the system" | [Singleton](../lld/patterns/singleton.md), usually declined | build once in `main` and inject | [Parking lot](../lld/problems/parking-lot.md) |

### Structural: something wraps or composes

| Symptom you hear | Pattern | Python idiom | Worked in |
|---|---|---|---|
| A vendor SDK's names and error codes leak inward | [Adapter](../lld/patterns/adapter.md) | thin wrapper over a `Protocol` | [Payment gateway](../lld/problems/payment-gateway-wallet.md) |
| Retry, rate limit and metrics must stack in any order | [Decorator](../lld/patterns/decorator.md) | `@decorator`, `functools.wraps` | [Notification service](../lld/problems/notification-service.md) |
| Access must be checked, delayed or cached before the real call | [Proxy](../lld/patterns/proxy.md) | `__getattr__` delegation, `cached_property` | [Library management](../lld/problems/library-management.md) |
| One user action needs six collaborators called in order | [Facade](../lld/patterns/facade.md) | one module-level function | [Amazon order flow](../lld/problems/ecommerce-order-inventory.md) |
| Leaf and container must answer the same question | [Composite](../lld/patterns/composite.md) | recursive dataclasses, `__iter__` | [In-memory file system](../lld/problems/in-memory-file-system.md) |
| Millions of objects repeat a handful of values | [Flyweight](../lld/patterns/flyweight.md) | interning, `lru_cache`, `Enum` | [Chess](../lld/problems/chess.md) |
| Two axes vary independently (kind by channel) | [Bridge](../lld/patterns/bridge.md) | injected implementor | [Notification service](../lld/problems/notification-service.md) |

### Behavioural: something decides, reacts or remembers

| Symptom you hear | Pattern | Python idiom | Worked in |
|---|---|---|---|
| One operation, several interchangeable rules chosen by the caller | [Strategy](../lld/patterns/strategy.md) | callables, `sorted(key=)` | [Parking lot](../lld/problems/parking-lot.md) |
| Behaviour depends on a status the object moves itself through | [State](../lld/patterns/state.md) | `Enum` plus a transition table | [Vending machine](../lld/problems/vending-machine.md) |
| "Update the display whenever the score changes" | [Observer](../lld/patterns/observer.md) | list of callbacks | [Cricinfo](../lld/problems/cricinfo.md) |
| Peers coordinate and the rules live nowhere | [Mediator](../lld/patterns/mediator.md) | one coordinator object | [Elevator system](../lld/problems/elevator-system.md) |
| Publisher and subscriber must never import each other | [Event Bus](../lld/patterns/event-bus.md) | `defaultdict(list)` keyed by topic | [Pub/sub queue](../lld/problems/pub-sub-system.md) |
| Undo, redo, replay or an audit trail of actions | [Command](../lld/patterns/command.md) | do and undo callable pairs | [Text editor](../lld/problems/text-editor.md) |
| A fixed sequence of steps whose middles differ | [Template Method](../lld/patterns/template-method.md) | `ABC` with hook methods | [Tic-tac-toe](../lld/problems/tic-tac-toe.md) |
| Rules tried in order until one handles it | [Chain of Responsibility](../lld/patterns/chain-of-responsibility.md) | list of callables | [ATM](../lld/problems/atm.md) |
| Ordered stages that transform and may drop the request | [Pipeline and Middleware](../lld/patterns/pipeline-middleware.md) | `reduce` over callables | [Notification service](../lld/problems/notification-service.md) |
| New operations keep arriving over a stable structure | [Visitor](../lld/patterns/visitor.md) | `singledispatch`, `match` | [In-memory file system](../lld/problems/in-memory-file-system.md) |
| Callers must traverse without seeing the container | [Iterator](../lld/patterns/iterator.md) | generators, `itertools` | [In-memory file system](../lld/problems/in-memory-file-system.md) |
| Restore an earlier state exactly, including what was deleted | [Memento](../lld/patterns/memento.md) | frozen dataclass, `deepcopy` | [Key-value store](../lld/problems/kv-store-transactions.md) |
| Users type filters or a small query syntax | [Interpreter](../lld/patterns/interpreter.md) | tokenizer plus `match` | [Stack Overflow](../lld/problems/stack-overflow.md) |

### Infrastructure edges: persistence, time and composition

| Symptom you hear | Pattern | Python idiom | Worked in |
|---|---|---|---|
| Domain logic must be tested without a database | [Repository](../lld/patterns/repository.md) | `Protocol` plus a dict-backed fake | [Library management](../lld/problems/library-management.md) |
| Several writes must land together or not at all | [Unit of Work](../lld/patterns/unit-of-work.md) | `contextmanager` around a working copy | [Payment gateway](../lld/problems/payment-gateway-wallet.md) |
| Tests need a fake clock, id generator or gateway | [Dependency Injection](../lld/patterns/dependency-injection.md) | constructor arguments typed as `Protocol` | every problem here |
| `if x is None` guards are spreading through callers | [Null Object](../lld/patterns/null-object.md) | a do-nothing implementation of the interface | [Logging framework](../lld/problems/logging-framework.md) |
| Business rules get combined with and, or, not | [Specification](../lld/patterns/specification.md) | predicates with `__and__`, `__or__` | [Stack Overflow](../lld/problems/stack-overflow.md) |
| Creating the resource costs far more than using it | [Object Pool](../lld/patterns/object-pool.md) | `queue.Queue` plus a context manager | [Car rental](../lld/problems/car-rental.md) |

### The confusion pairs, and the single question that settles each

| Group | Ask yourself | Then say |
|---|---|---|
| Strategy, State, Template Method | *Who picks the behaviour, and when?* | The caller injects one of several peers that ignore each other (Strategy); the object swaps its own delegate as events arrive and each state names its successors (State); a base class fixes the skeleton and subclasses fill steps, decided when the class is written (Template Method). |
| Decorator, Proxy, Adapter | *Does the interface change, and what happens to the call?* | Same interface, always forwards, adds behaviour, stacks (Decorator); same interface, decides *whether* the real call happens for lazy loading, caching or permissions (Proxy); different interface in, no behaviour added, one-to-one over something you do not own (Adapter). Adapt first, then decorate. |
| Observer, Mediator, Event Bus | *Who knows whom?* | The subject holds its listeners and calls them (Observer); colleagues know only a coordinator that owns the rules (Mediator); both sides know only a topic string and dispatch may be asynchronous (Event Bus). |
| Facade, Adapter | *Simpler, or compatible?* | A facade invents a smaller interface over many objects so one call replaces six; an adapter supplies an *existing* interface over one object so old clients keep working. A facade may contain adapters; an adapter never simplifies. |
| Composite, Decorator | *How many children?* | Composite aggregates many children to answer as a whole; a decorator has exactly one child and exists to add behaviour. |
| Chain of Responsibility, Pipeline | *May a link refuse to forward?* | A chain stops at the first handler that copes; a pipeline runs every stage in order and returns a value, though a stage may short-circuit. |

### When the honest answer is no pattern

| Tempting pattern | Cheaper answer that scores the same or better |
|---|---|
| Singleton for a manager | one instance created at the composition root and injected |
| Strategy with exactly one implementation | a plain method, plus a sentence on where the seam would go |
| Builder for three required fields | a frozen dataclass with keyword-only arguments |
| Observer between two objects | a direct call, until a third listener exists |
| Abstract Factory for one family | a module of functions |
| Visitor over two node types | `match` on the type in one function |

## Memory hooks

- **"Name the axis before the pattern."** If you cannot say what will change, the answer is no pattern.
- **Strategy is *what*, State is *when*, Template Method is *where*.** Rule, lifecycle, step.
- **Decorator adds, Proxy withholds, Adapter translates.** Identical boxes, three intents.
- **Observer is one-to-many, Mediator is many-to-one, Event Bus is many-to-many.**
- **Command is *what to do*, Observer is *whom to tell*, Event is *what happened*.**
- **Two implementations justify a seam; one is speculation.** Say the second one out loud.

!!! tip "Interview tip"
    Say the pattern *after* the symptom and *before* the class name: "the split rule is what they will change, so it goes behind a `SplitStrategy` and the group only calls `split`; a percentage split becomes one new class and one test." That sentence is what the rubric rewards.

!!! warning "Common mistake"
    Announcing a pattern list in the first minute, then bending the design to fit it. Interviewers read that as vocabulary without judgement. Draw the Gang of Four shape when asked, then say which Python idiom you would really write, and name the pattern you deliberately declined.

## Related

- [Design patterns overview](../lld/patterns/patterns-overview.md) — the same map with intent, families and the decision flowchart
- [Strategy](../lld/patterns/strategy.md) — learn it first; every Pythonic variant is a version of it
- [State](../lld/patterns/state.md) — Strategy's most frequent confusion, with the transition table
- [Observer](../lld/patterns/observer.md) — the pattern behind every "notify" requirement
- [LLD round checklist](lld-checklist.md) — where pattern talk belongs in the 45 minutes
- [The LLD interview framework](../lld/fundamentals/lld-interview-framework.md) — requirements first, patterns last
