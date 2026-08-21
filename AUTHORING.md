# AUTHORING.md — the style guide every author reads first

This handbook is written for an SDE2 candidate preparing for FAANG-style HLD (system design) and LLD (object-oriented design) rounds. Every page must make the reader better *in the room*: what to say, what to draw, what to write, and why.

## 1. Voice and depth
- Senior-mentor tone, second person ("you"), direct. Explain **why**, quantify claims, prefer tables for comparisons.
- No filler openers ("In today's world…"), no marketing adjectives, no emojis, no "as an AI", no "we will explore".
- Vendor-neutral unless naming a technology *is* the point (Kafka, Redis, Cassandra, DynamoDB are fine as concrete examples).
- Every claim with a number must come from `docs/cheatsheets/latency-and-estimation.md` (the canonical numbers page). Show your arithmetic.
- QPS convention: divide by `10^5` and say so ("a day is ~10^5 s"). Apply the x1.15 exactness correction only when a page's argument turns on it, and name it when you do — otherwise two pages turn the same input into different QPS.
- Use the terms in `GLOSSARY.md`. The linter fails pages that use banned terms.
- Original wording only. Never paste text from books, blogs or papers; cite primary sources in `Related` instead.

## 2. Page anatomy
- Front matter with `title` and `description` (one sentence). Exactly one H1 equal to `title`.
- Then **exactly** the H2 set for the category, in the order given by the template in `docs/_templates/`. H3s are free. Delete every `<!-- T: … -->` comment.
- The last H2 is always `## Related` with 3–6 relative links to catalogue pages (`../fundamentals/caching-and-cdn.md`), link text = page title, plus up to 3 external **primary** sources (papers, official docs). Link only to slugs that exist in `CATALOGUE.md`.
- Admonitions: `!!! tip "Interview tip"`, `!!! warning "Common mistake"`, `!!! note`, `??? question "…"` (collapsible), `??? example "…"`. Every content page has at least one tip and one common-mistake admonition; at most 3 `!!!` admonitions per page (collapsible `???` blocks are not counted). No nested admonitions.
- Content tabs (`=== "Tab"`) are allowed for alternative implementations; indent nested fences by 4 spaces.

## 3. Targets per category

| Category | Words (excl. code/diagrams) | Diagrams (min) | Code |
|---|---|---|---|
| HLD fundamental | 1500–2500 | 1 (as listed in the row) | optional single-file module in `code/hld/` |
| HLD case study | 2200–3200 | 5: v1 flowchart, v2 flowchart, erDiagram, write-path sequence, read-path sequence (+ row extras) | exactly one key snippet module in `code/hld/` |
| LLD fundamental | 1200–2000 | 0–1 | small module in `code/fundamentals/` |
| Design pattern | 800–1300 | 1 classDiagram | `code/patterns/<name>.py` + ≥3 tests |
| LLD problem | 1800–2800 | classDiagram + sequenceDiagram (+ stateDiagram-v2 when an entity has ≥3 statuses) | full package `code/lld/<pkg>/` + ≥5 tests |
| Cheatsheet | 600–1500 | 0–1 | none |
| Mock interview | 2500–3500 | 2–3 (v1/v2/v3 of the same design) | none (links to existing code) |

## 4. Python conventions (ruff-checked; reviewers enforce the rest)
- Python 3.12, **standard library only**. Type hints on every signature; `X | None`, not `Optional[X]`.
- `@dataclass(frozen=True, slots=True)` for value objects; `@dataclass(slots=True)` for mutable entities; `Enum`/`StrEnum` for states; `ABC` for shared behaviour, `Protocol` for pure interfaces.
- Inject time and IDs: `from common import Clock, SystemClock, FakeClock, IdGenerator, SequentialIdGenerator`. Never call `time.time()`, `datetime.now()` or `uuid4()` inside services. Money is `common.Money` (integer cents) or `Decimal`; never float.
- Domain exceptions subclass `common.HandbookError` (or `ValidationError`, `NotFoundError`, `ConflictError`, `InvalidStateError`).
- Shared mutable state is guarded by `threading.Lock`/`RLock`; document which lock protects what. Acquire multiple locks in a fixed order (e.g. by id).
- No `print` outside `demo.py` / `if __name__ == "__main__":` blocks. No mutable default arguments. No module-level mutable state. Modules ≤400 lines (up to 500 for `code/fundamentals/` tour modules); split when larger.
- Layout: LLD problem = package `code/lld/<pkg>/` with `__init__.py` (re-exports), `models.py`, `services.py` (or a domain-specific name such as `engine.py`), `demo.py` (`main()` prints a 10–20 line scenario in <2 s), `tests/__init__.py`, `tests/test_<pkg>.py`. Single-file artifacts: `code/hld/<name>.py`, `code/patterns/<name>.py`, `code/fundamentals/<name>.py` with tests in the sibling `tests/test_<name>.py`. Test file basenames must be unique across the repo.
- Imports are `from lld.parking_lot.models import …`, `from hld.consistent_hashing import …`, `from patterns.strategy import …`, `from common import …` — never `code.` as a prefix (`code` is a stdlib module and is **not** a package here).
- Tests: plain pytest functions, `@pytest.mark.parametrize`, deterministic (FakeClock, seeded `random.Random(42)`, no sleeps >50 ms), one concurrency test with `ThreadPoolExecutor` wherever a lock exists. LLD problems: ≥5 meaningful tests (happy path, validation error, state transition, concurrency, edge case). Patterns/HLD modules: ≥3.
- Run before returning: `uv run ruff check <your paths>` and `uv run pytest <your paths> -q`.

## 5. Single-sourced code (snippets)
Any Python block longer than 12 lines **must** be an include of a real file, introduced by 1–3 sentences that tell the reader what to look at:

````markdown
```python title="code/lld/parking_lot/models.py"
--8<-- "code/lld/parking_lot/models.py"
```
````

For files longer than ~150 lines, embed sections. In the code:

```python
# --8<-- [start:pricing]
class PricingStrategy(Protocol): ...
# --8<-- [end:pricing]
```

and in the page: `--8<-- "code/lld/parking_lot/services.py:pricing"`. Section names are `snake_case`; keep marker lines at column 0 or on their own line inside the class body. Short illustrative fragments (≤12 lines) may be written inline.

Demo output is shown as a ```` ```text ```` block and must match what `uv run python -m lld.<pkg>.demo` prints.

## 6. Diagram conventions (Mermaid)
Allowed types: `flowchart LR` / `flowchart TD` (architecture, request paths, decision trees), `sequenceDiagram` (flows), `classDiagram` (LLD structure), `stateDiagram-v2` (lifecycles), `erDiagram` (data models). Nothing else. Always `flowchart`, never `graph`.

One diagram per fence; a **bold caption sentence** on the line above the fence; blank lines before and after. Soft limit 25 nodes, hard limit 30 (split the diagram instead). `%%` comments only on their own line. Never use `%%{init …}%%`, `style`, `classDef`, `click` (they break the dark theme). No `"` inside quoted labels (rephrase). ASCII ids only. No tabs. No unicode arrows.

### Things that silently break rendering — follow exactly
- **classDiagram**: no `[`, `]`, `|`, `<`, `>` in member types. Write `List~Vehicle~`, `Dict~str,int~`, `Ticket` (not `Optional[Ticket]`), return types after the signature: `+park(vehicle: Vehicle) Ticket`. Class names PascalCase, no spaces/hyphens, and **identical to the code**. Annotations `<<interface>>`, `<<abstract>>`, `<<enumeration>>`. Relations: `<|--` inheritance, `*--` composition, `o--` aggregation, `-->` association, `..>` dependency, `<|..` realization (equivalently `..|>` written right-to-left; pages use `<|..`), with optional multiplicities `Lot "1" *-- "many" Floor`.
- **flowchart**: quote any label with punctuation: `api["API Gateway (L7)"]`. Never name a node `end`. Prefix ids (`svc_order`, `db_users`) so no id starts with `o` or `x` after `---`. Edge labels `-->|"label"|` without `|` inside. `subgraph cache["Cache tier"] … end`. Stores as cylinders `db[("Postgres")]`, queues `q[["Kafka"]]`. Line breaks only via `<br/>` inside quoted labels.
- **sequenceDiagram**: participants are single tokens with aliases: `participant LB as Load Balancer`, `actor U as User` (no quotes). `->>` sync, `-->>` reply, `-)` async. Balanced `activate`/`deactivate` (or `+`/`-`). No `;` in message text. `Note over A,B: text`. Blocks `alt/else/end`, `opt/end`, `loop/end`, `par/and/end`. `autonumber` on the first line. ≤8 participants.
- **erDiagram**: relationship label is mandatory: `USER ||--o{ ORDER : places`. Entity names `UPPER_SNAKE`. Attributes `type name PK "comment"` with single-token types (`uuid`, `string`, `int`, `bigint`, `timestamp`, `decimal`, `json`). ≤10 entities.
- **stateDiagram-v2**: `[*] --> Idle`, `Idle --> Active : coin_inserted`. Names with spaces only via `state "Waiting for payment" as WaitPay`. No `--` inside names.

Validate before returning: `node scripts/validate_mermaid.mjs --files <your pages>`.

## 7. Cross-links and images
- Relative links only (`../../lld/patterns/strategy.md`); never absolute URLs to this site; never link to a page that is not in `CATALOGUE.md`.
- Images: only the generated figures under `docs/assets/img/` that already exist. Reference them as `![Alt text](../../assets/img/figures/hash_ring.png){ width="800" }`. Do not invent image paths.

## 8. Do-not list (review fails on any of these)
Placeholders (`TODO`, `TBD`, `FIXME`, `lorem`, `coming soon`, `<!-- T:`); "as an AI"; links or snippet paths to files that do not exist; verbatim copyrighted text; files >600 lines; diagrams >30 nodes; disallowed diagram types; `print` debugging in library code; sleeps in tests; mutable default args; editing files outside your allowlist; running `uv add`, `uv sync`, `uv lock`, `pip`, `git commit`; running `mkdocs build` without `-d .build/<your-batch>`.

## 9. Before you return
1. `uv run ruff check <paths>` — clean.
2. `uv run pytest <paths> -q` — green; `uv run python -m <demo module>` — exits 0 in <2 s.
3. `uv run python scripts/lint_pages.py --planned <pages>` — 0 errors.
4. `node scripts/validate_mermaid.mjs --files <pages>` — 0 failures.
5. `git status --porcelain` — every changed path is inside your allowlist.
6. Return the report in the exact format requested by your prompt.
