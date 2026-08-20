#!/usr/bin/env python3
"""Render an authoring or reviewing prompt from scripts/prompt_templates/*.md.

Fills every {{PLACEHOLDER}} from CATALOGUE.md and prints the prompt to stdout
(or writes it with --out). Exits 1 if a placeholder would be left unfilled.

Usage:
  python scripts/render_prompt.py --batch HLD-C5
  python scripts/render_prompt.py --batch LLD-L1 --only elevator-system
  python scripts/render_prompt.py --ids LP-02,LP-03
  python scripts/render_prompt.py --reviewer R1 --pages HC-01,HC-02
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).parent))
import catalogue  # noqa: E402

ROOT = catalogue.ROOT
TEMPLATES = ROOT / "scripts" / "prompt_templates"
FIGURES_DIR = "docs/assets/img/figures"
UV = "~/.local/bin/uv"
PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")

NEWS_FEED = ("docs/hld/case-studies/news-feed.md", "code/hld/fanout.py")
PARKING_LOT = ("docs/lld/problems/parking-lot.md", "code/lld/parking_lot/")
STRATEGY = ("docs/lld/patterns/strategy.md", "code/patterns/strategy.py")
HASHING = ("docs/hld/fundamentals/partitioning-and-consistent-hashing.md", "code/hld/consistent_hashing.py")
LATENCY = ("docs/cheatsheets/latency-and-estimation.md", "-")
# category -> (preferred golden pair, fallback pair used while the preferred page is missing)
GOLDEN: dict[str, tuple[tuple[str, str], tuple[str, str] | None]] = {
    "hld-fundamental": (HASHING, NEWS_FEED),
    "hld-case-study": (NEWS_FEED, None),
    "design-pattern": (STRATEGY, PARKING_LOT),
    "lld-problem": (PARKING_LOT, None),
    "lld-fundamental": (STRATEGY, PARKING_LOT),
    "cheatsheet": (LATENCY, None),
    "mock-interview": (NEWS_FEED, None),
}
KIND_NAME = {
    "flowchart": "flowchart",
    "sequence": "sequenceDiagram",
    "class": "classDiagram",
    "state": "stateDiagram-v2",
    "er": "erDiagram",
}
STANDARD_SET = "v1 flowchart, v2 flowchart, erDiagram, write-path sequenceDiagram, read-path sequenceDiagram"


# ----------------------------------------------------------------------------- helpers
def rel_link(from_page: str, to_path: str) -> str:
    start = PurePosixPath(from_page).parent
    return os.path.relpath(to_path, start=str(start)).replace(os.sep, "/")


def golden_pair(category: str) -> tuple[str, str]:
    preferred, fallback = GOLDEN[category]
    if fallback and not (ROOT / preferred[0]).is_file():
        return fallback
    return preferred


def counted_kinds(kinds: list[str]) -> str:
    """['class', 'class', 'sequence'] -> 'classDiagram x2, sequenceDiagram'."""
    counts = Counter(kinds)
    parts = []
    for kind in dict.fromkeys(kinds):
        name = KIND_NAME.get(kind, kind)
        parts.append(f"{name} x{counts[kind]}" if counts[kind] > 1 else name)
    return ", ".join(parts)


def diagram_requirement(page: catalogue.Page) -> str:
    kinds = [d for d in page.diagrams if not d.startswith("fig:")]
    cat = page.category
    if cat == "hld-case-study":
        text = f"minimum {5 + len(kinds)} Mermaid fences: the standard set of 5 ({STANDARD_SET})"
        if kinds:
            text += f" + extras: {counted_kinds(kinds)}"
        return text + "."
    if cat == "hld-fundamental":
        minimum = max(page.mermaid_min, len(kinds))
        text = f"minimum {minimum} Mermaid fence(s)"
        if kinds:
            text += f"; required kinds: {counted_kinds(kinds)}"
        else:
            text += " (any allowed type that fits the scope)"
        return text + "."
    if cat == "design-pattern":
        extras = [k for k in kinds if k != "class"]
        text = ">=1 classDiagram (class names identical to the code)"
        if extras:
            text += f" + {counted_kinds(extras)}"
        return text + "."
    if cat == "lld-problem":
        text = ">=1 classDiagram + >=1 sequenceDiagram (+ stateDiagram-v2 when an entity has >=3 statuses)"
        if "state" in kinds:
            text += "; the catalogue expects the stateDiagram-v2 for this problem"
        return text + "."
    if cat == "lld-fundamental":
        if kinds:
            return f"{len(kinds)} Mermaid fence(s) from the catalogue: {counted_kinds(kinds)}."
        return "0-1 diagrams (none required)."
    if cat == "cheatsheet":
        return "none required (0-1 allowed); this page is tables."
    if cat == "mock-interview":
        return f"three evolving diagrams of the same design (v1 skeleton, v2, v3 after deep dives): {counted_kinds(kinds) or 'flowchart x3'}."
    return f"minimum {page.mermaid_min} Mermaid fence(s)."


def figure_lines(page: catalogue.Page) -> list[str]:
    figs = [d[4:] for d in page.diagrams if d.startswith("fig:")]
    if not figs:
        return []
    lines = ["- figures to embed (generated images under docs/assets/img/figures/; never invent paths):"]
    for fig in figs:
        rel_repo = f"{FIGURES_DIR}/{fig}.png"
        status = "exists" if (ROOT / rel_repo).is_file() else "MISSING on disk right now - embed it anyway and list it under REQUESTS if still missing when you finish"
        embed = f'![{fig.replace("_", " ").capitalize()}]({rel_link(page.path, rel_repo)}){{ width="800" }}'
        lines.append(f"  - {rel_repo} ({status}); embed syntax from this page: `{embed}`")
    return lines


def code_lines(page: catalogue.Page) -> list[str]:
    if not page.code:
        note = "none - no new Python for this page"
        if page.category == "mock-interview":
            note += "; link the existing case-study/problem page and its code package"
        if page.category == "cheatsheet":
            note += "; no code blocks"
        return [f"- code: {note}"]
    lines = []
    for pkg in page.packages:
        files = [f.removeprefix(pkg) for f in page.required_files() if f.startswith(pkg)]
        lines.append(
            f"- code: package {pkg} with files: {', '.join(files)}"
            f" (any additional .py files inside {pkg} are allowed, e.g. strategies.py)"
        )
    for mod in page.modules:
        test = next(f for f in page.required_files() if f.startswith(str(Path(mod).parent / "tests" / f"test_{Path(mod).stem}")))
        lines.append(f"- code: {mod}  + tests: {test}")
    return lines


def links_line(page: catalogue.Page, by_slug: dict[str, catalogue.Page]) -> str:
    targets = []
    for slug in page.links_to:
        target = by_slug[slug]
        targets.append(f"{target.title} -> {rel_link(page.path, target.path)}")
    return "- must link (in ## Related, link text = page title): " + " ; ".join(targets)


def assignment_block(n: int, page: catalogue.Page, by_slug: dict[str, catalogue.Page]) -> str:
    lo, hi = page.word_range
    lines = [
        f'### {n}. {page.id} {page.slug} - "{page.title}"',
        f"- page: {page.path}   (template: {page.template})",
        *code_lines(page),
        f"- diagrams: {diagram_requirement(page)}",
        *figure_lines(page),
        f"- tier: {page.tier}   weight: {page.weight}   words: {lo}-{hi} (excluding code and diagrams)",
        links_line(page, by_slug),
        f"- scope: {page.scope}",
    ]
    if page.brief:
        brief_lines = page.brief.splitlines()
        lines.append(f"- brief: {brief_lines[0]}")
        lines += [f"  {line}" for line in brief_lines[1:]]
    return "\n".join(lines)


def sibling_note(batch: str, selected: list[catalogue.Page], siblings: list[catalogue.Page]) -> str:
    if not siblings:
        return ""
    lines = [
        f"Re-spawn note: you are writing only {', '.join(p.slug for p in selected)} from batch {batch}. "
        "The sibling pages of this batch already exist (or are being written); read them for consistency "
        "(terminology, shared base classes, cross-links) but do not modify them:",
    ]
    for p in siblings:
        status = "exists" if (ROOT / p.path).is_file() else "not on disk yet"
        lines.append(f"- {p.path} ({status})" + (f"; code: {', '.join(p.code)}" if p.code else ""))
    return "\n".join(lines) + "\n"


def allowlist(pages: list[catalogue.Page], batch_id: str) -> str:
    lines = []
    for page in pages:
        for f in page.required_files():
            lines.append(f"- {f}")
        for pkg in page.packages:
            lines.append(f"- any additional .py files inside {pkg} are allowed (keep each module <=400 lines)")
    lines.append(f"- .build/{batch_id}/  (scratch output for `mkdocs build -d .build/{batch_id}`; never `site/`)")
    return "\n".join(lines)


COMMON_REQUIREMENTS = [
    "Front matter with `title` and `description` (one sentence); exactly one H1 equal to `title`; then exactly the H2 set of the template, in order (H3s are free); delete every `<!-- T: ... -->` comment.",
    "The last H2 is `## Related`: 3-6 relative links to catalogue pages (link text = page title) plus up to 3 primary external sources; it must include every 'must link' target listed in your assignment. Link only to slugs that exist in CATALOGUE.md.",
    "Admonitions: at least one `!!! tip \"Interview tip\"` and one `!!! warning \"Common mistake\"`; at most 3 `!!!` admonitions per page (collapsible `???` blocks do not count); no nested admonitions.",
    "Word count within the range given above (prose only; code blocks and diagrams excluded). Senior-mentor tone, second person, no filler, no emojis, original wording.",
    "Every number comes from docs/cheatsheets/latency-and-estimation.md and shows its arithmetic; use GLOSSARY.md terms (the linter fails banned terms such as master/slave, whitelist/blacklist).",
    "Mermaid: only `flowchart LR|TD`, `sequenceDiagram`, `classDiagram`, `stateDiagram-v2`, `erDiagram`; one diagram per fence; a bold caption sentence on the line above the fence; <=25 nodes (hard limit 30); no `%%{init}`, `style`, `classDef`, `click`; quote labels with punctuation; follow the pitfall list in AUTHORING.md section 6 exactly.",
    "Any Python block longer than 12 lines must be a `--8<--` include of a real file (`--8<-- \"code/...\"`, or a `[start:name]`/`[end:name]` section for files >150 lines); demo output goes in a ```text block and must match what the demo actually prints.",
    "Python: 3.12, stdlib only, type hints everywhere, `X | None`; imports are `from lld.<pkg>... import`, `from hld.<mod> import`, `from patterns.<mod> import`, `from common import` (never a `code.` prefix); no `print` outside demo/`main()`; no mutable defaults; no module-level mutable state.",
    "No placeholders (TODO, TBD, FIXME, lorem, coming soon); no files outside your allowlist; never `uv add/sync/lock`, `pip`, `git commit`; `mkdocs build` only with `-d .build/<batch>`.",
]

CATEGORY_REQUIREMENTS: dict[str, list[str]] = {
    "lld-problem": [
        "Package layout `code/lld/<pkg>/`: `__init__.py` re-exports the public names (so docs can say `from lld.<pkg> import X`); `models.py` holds enums (`StrEnum`), frozen value objects (`@dataclass(frozen=True, slots=True)`), entities (`@dataclass(slots=True)`) and domain exceptions subclassing `common.HandbookError` (or ValidationError/NotFoundError/ConflictError/InvalidStateError); `services.py` holds the managers/services and the locks (you may add `engine.py`/`strategies.py`, but `services.py` must exist); `demo.py` with `main()` printing a 10-20 line scenario in <2 s; `tests/__init__.py`; `tests/test_<pkg>.py`. Modules <=400 lines - split when larger.",
        "Inject time and ids: `from common import Clock, SystemClock, FakeClock, IdGenerator, SequentialIdGenerator`; never call `time.time()`, `datetime.now()` or `uuid4()` inside services. Money is `common.Money` (integer cents) or `Decimal`, never float.",
        "Shared mutable state is guarded by `threading.Lock`/`RLock`; the page states which lock protects what and the race it prevents; acquire multiple locks in a fixed order (e.g. by id).",
        ">=5 meaningful tests in `tests/test_<pkg>.py`: happy path, validation error, state transition, concurrency with `ThreadPoolExecutor` (assert the invariant, e.g. one winner for the last spot), edge case; plain pytest functions, `@pytest.mark.parametrize` where natural, deterministic (FakeClock, `random.Random(42)`, no sleeps >50 ms).",
        "Class names in the classDiagram are identical to the classes in the code (PascalCase, no spaces); <=25 nodes per diagram - split into two diagrams if needed. Add a stateDiagram-v2 when an entity has >=3 statuses.",
        "`## Requirements` enumerates the FR bullets of the brief; `## Clarifying questions and assumptions` is a table; `## Concurrency and edge cases` covers the brief's hot spots; `## Extensibility and follow-ups` covers the brief's follow-ups; `## Design patterns applied` is a table (pattern | where | why) that also says what you deliberately did not use.",
        "`## Implementation` embeds models.py then services.py (sections) in the order you would write them in the interview, each introduced in 1-3 sentences, and ends with demo.py plus its output in a ```text block that matches `python -m lld.<pkg>.demo`. `## Tests` explains the cases and embeds 1-2 tests as snippets.",
        "`## 45-minute pacing` is a table (minutes | what to say/draw/write) specific to this problem.",
    ],
    "hld-case-study": [
        "Diagrams: the standard set of 5 - v1 architecture `flowchart` (subgraphs Clients / Edge / Services / Async / Data) under `## High-level design`, write-path and read-path `sequenceDiagram`s (`autonumber`, <=8 participants, narrated) under `## High-level design`, an `erDiagram` under `## Data model` (relationship labels mandatory, UPPER_SNAKE entities, <=10 entities), a v2 `flowchart` under `## Scaling, bottlenecks and failure modes` - plus the row extras listed in your assignment.",
        "Exactly one snippet module `code/hld/<name>.py` implementing the crux named in the scope, with a `main()` demo under `if __name__ == \"__main__\":`, `# --8<-- [start:x]` markers, and >=3 meaningful tests in `code/hld/tests/test_<name>.py` (one concurrency test with `ThreadPoolExecutor` if the module holds a lock). Embed it with `--8<--` inside the deep dive it belongs to.",
        "`## Estimation` uses only numbers from docs/cheatsheets/latency-and-estimation.md and shows the arithmetic: a table with read QPS, write QPS, storage/year, bandwidth, cache size (peak = 3x average unless stated).",
        "`## Problem statement and clarifying questions`: one framing paragraph + a table of 6-10 questions with the assumed answers. `## Requirements` has H3s Functional / Non-functional (with numbers: scale, latency targets, consistency, durability, availability) / Out of scope.",
        "`## API design`: 3-6 endpoints with request/response shapes, idempotency and pagination notes. `## Data model`: erDiagram + store choice + partition/sort keys + indexes.",
        "3-5 `## Deep dive: <crux>` H2s of 250-400 words each: the probing question, an options table, the chosen approach and why, with a diagram or the key Python snippet. Use the cruxes named in the scope.",
        "`## Trade-offs summary` table (decision | chosen | alternatives | why); `## Interviewer follow-ups` with 6-8 `??? question \"...\"` blocks; `## 45-minute pacing` table specific to this problem.",
    ],
    "hld-fundamental": [
        "`## Core concepts`: one H3 per subtopic in the scope, 900-1400 words in total, with the required diagrams placed here (bold caption line, then the fence) and quantified claims.",
        "Code artifacts listed in your assignment: each module has a `main()` demo under `if __name__ == \"__main__\":`, `# --8<-- [start:x]` markers and >=3 meaningful tests in `code/hld/tests/test_<name>.py` (deterministic; a `ThreadPoolExecutor` test wherever a lock exists). `## Python implementation` exists only when the row lists code - delete that H2 entirely otherwise; when present, introduce each snippet in 1-3 sentences, embed with `--8<--`, then show the demo output in a ```text block.",
        "Figures (`fig:` entries) are embedded with the exact relative path given in your assignment and `{ width=\"800\" }`; never invent image paths.",
        "`## TL;DR`: 3-5 bullets (<=80 words). `## Trade-offs`: one comparison table (options x criteria) + 150-300 words on when to choose what.",
        "`## In the interview`: how to introduce the concept in a design, 2-3 phrases that signal depth, and 5 follow-up questions with model answers as `??? question \"...\"`, plus exactly one `!!! tip \"Interview tip\"`. `## Common mistakes`: 4-6 entries (symptom -> why it costs -> fix) with exactly one `!!! warning \"Common mistake\"`. `## Self-check`: 5 `??? question \"...\"` blocks with answers.",
    ],
    "design-pattern": [
        "Module `code/patterns/<name>.py` with the participants named exactly as in the classDiagram, a `main()` demo under `if __name__ == \"__main__\":`, `# --8<-- [start:x]` markers, and >=3 meaningful tests in `code/patterns/tests/test_<name>.py`.",
        "`## Structure`: >=1 classDiagram with participant roles (<=60 words of prose); class names identical to the code. `## Canonical example in Python` embeds the module via `--8<--` (sections if >150 lines), walks through it, then shows the demo output in a ```text block.",
        "`## Pythonic variant`: the idiomatic alternative (functions, closures, generators, decorators, dataclasses, stdlib) in <=12-line inline snippets, and when it is enough. `## Real-world usage`: where it appears in the stdlib/frameworks.",
        "`## Related patterns and confusions` tells the pattern apart from its neighbours (e.g. Strategy vs State vs Template Method); `## Where it appears in LLD problems` links the LLD problem pages named in the scope/links; `## Interview tips` has exactly one `!!! tip \"Interview tip\"` and one `!!! warning \"Common mistake\"`.",
        "DP-00 (patterns-overview) only: the scope lists the H2s that are required; the others may be omitted.",
    ],
    "lld-fundamental": [
        "Module `code/fundamentals/<name>.py` with the before/after examples as small classes/functions, `# --8<-- [start:x]` markers, a `main()` under `if __name__ == \"__main__\":`, and >=3 meaningful tests in `code/fundamentals/tests/test_<name>.py`.",
        "`## Concepts`: one H3 per subtopic in the scope (800-1400 words), each with a short before (smell) and after (fix) Python example where that makes sense; diagrams inline with bold captions (kinds listed in your assignment).",
        "`## Applying it in the interview`: what to say, when to invoke it, how it shows up in the LLD framework, with exactly one `!!! tip \"Interview tip\"`. `## Pitfalls`: 4-6 pitfalls with exactly one `!!! warning \"Common mistake\"`. `## Exercises`: 3-5 exercises with solutions collapsed in `??? example \"Solution\"`.",
    ],
    "cheatsheet": [
        "Tables, not prose: `## How to use this sheet` (<=60 words), `## Tables` with one H3 per table (dense, scannable), `## Memory hooks` (mnemonics and one-liners), `## Related` linking the pages that explain each row.",
        "No code blocks and no new code artifacts; 0-1 diagrams. Numbers must agree with docs/cheatsheets/latency-and-estimation.md (do not introduce contradicting figures).",
        "The tip/common-mistake admonitions still apply (one of each, at most 3 `!!!` blocks).",
    ],
    "mock-interview": [
        "No new code: `## Artifacts` links the existing case-study/problem page and its code package (paths from CATALOGUE.md); LLD mocks show the order in which methods were written and the real `pytest` output of the existing package.",
        "`## Setup`: role/level, the prompt exactly as the interviewer states it, what is being graded. `## Timeline`: table (t | phase | interviewer says | candidate says/draws/writes | artifact).",
        "`## Transcript`: full dialogue as `> **Interviewer:**` / `> **Candidate:**` blockquotes, with three evolving Mermaid diagrams of the same design (v1 skeleton, v2 with async/caching or the second pass, v3 after the deep dives), kinds as listed in your assignment.",
        "`## Debrief`: rubric table (dimension | below bar | meets SDE2 | exceeds) with concrete quotes from the transcript. `## Practice variants`: 3 variants of the prompt to redo alone.",
    ],
}


def requirements(category: str) -> str:
    bullets = CATEGORY_REQUIREMENTS[category] + COMMON_REQUIREMENTS
    return "\n".join(f"- {b}" for b in bullets)


def verify_commands(pages: list[catalogue.Page]) -> str:
    cd = f'cd "{ROOT}" && '
    page_paths = " ".join(p.path for p in pages)
    code_paths: list[str] = []
    demos: list[str] = []
    for p in pages:
        code_paths += [pkg.rstrip("/") for pkg in p.packages]
        for mod in p.modules:
            code_paths.append(mod)
            code_paths.append(next(f for f in p.required_files() if "/tests/test_" in f and Path(f).stem == f"test_{Path(mod).stem}"))
        demos += p.demo_modules()
    cmds: list[str] = []
    if code_paths:
        joined = " ".join(code_paths)
        cmds.append(f"{cd}{UV} run ruff check {joined}")
        cmds.append(f"{cd}{UV} run pytest {joined} -q")
        for demo in demos:
            cmds.append(f"{cd}PYTHONPATH=code {UV} run python -m {demo}    # must exit 0 in <2 s; paste its output into the page")
    cmds.append(f"{cd}{UV} run python scripts/lint_pages.py --planned {page_paths}")
    cmds.append(f"{cd}node scripts/validate_mermaid.mjs --files {page_paths}")
    cmds.append(f"{cd}git status --porcelain    # every listed path must be in your allowlist (Step 3)")
    return "\n".join(f"{i}. `{c}`" for i, c in enumerate(cmds, 1))


# ----------------------------------------------------------------------------- selection
def select_rows(args: argparse.Namespace, pages: list[catalogue.Page]) -> tuple[str, list[catalogue.Page], list[catalogue.Page]]:
    """Returns (batch_id, selected rows, unselected siblings of the batch)."""
    ids = catalogue.by_id(pages)
    slugs = catalogue.by_slug(pages)
    if args.batch:
        rows = catalogue.by_batch(pages, args.batch)
        if not rows:
            raise SystemExit(f"render_prompt: no catalogue rows with batch {args.batch!r}")
        batch_id = args.batch
        siblings: list[catalogue.Page] = []
        if args.only:
            wanted = [s.strip() for s in args.only.split(",") if s.strip()]
            unknown = [w for w in wanted if w not in slugs or slugs[w].batch != args.batch]
            if unknown:
                raise SystemExit(f"render_prompt: --only slugs not in batch {args.batch}: {unknown}")
            siblings = [r for r in rows if r.slug not in wanted]
            rows = [r for r in rows if r.slug in wanted]
    else:
        wanted = [s.strip() for s in args.ids.split(",") if s.strip()]
        unknown = [w for w in wanted if w not in ids]
        if unknown:
            raise SystemExit(f"render_prompt: unknown ids: {unknown}")
        rows = [ids[w] for w in wanted]
        batches = {r.batch for r in rows}
        batch_id = batches.pop() if len(batches) == 1 else "ADHOC"
        siblings = []
    categories = sorted({r.category for r in rows})
    if len(categories) != 1:
        raise SystemExit(f"render_prompt: selected rows span several categories {categories}; one prompt covers one category (use --ids to split)")
    return batch_id, rows, siblings


def resolve_pages(spec: str, pages: list[catalogue.Page]) -> list[catalogue.Page]:
    ids = catalogue.by_id(pages)
    slugs = catalogue.by_slug(pages)
    by_path = {p.path: p for p in pages}
    found: list[catalogue.Page] = []
    for token in (t.strip() for t in spec.split(",") if t.strip()):
        path = token if token.startswith("docs/") else f"docs/{token}"
        page = ids.get(token) or slugs.get(token) or by_path.get(token) or by_path.get(path)
        if page is None:
            raise SystemExit(f"render_prompt: cannot resolve page {token!r} (use an id, slug or docs path)")
        found.append(page)
    return found


# ----------------------------------------------------------------------------- rendering
def fill(template_name: str, values: dict[str, str]) -> str:
    text = (TEMPLATES / template_name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    leftover = sorted(set(PLACEHOLDER.findall(text)))
    if leftover:
        raise SystemExit(f"render_prompt: unfilled placeholders in {template_name}: {leftover}")
    return text


def render_author(args: argparse.Namespace, pages: list[catalogue.Page]) -> str:
    batch_id, rows, siblings = select_rows(args, pages)
    by_slug = catalogue.by_slug(pages)
    category = rows[0].category
    golden_page, golden_code = golden_pair(category)
    assignments = sibling_note(batch_id, rows, siblings)
    assignments += "\n\n".join(assignment_block(n, row, by_slug) for n, row in enumerate(rows, 1))
    return fill(
        "author.md",
        {
            "BATCH_ID": batch_id,
            "CATEGORY": category,
            "TEMPLATE_FILE": rows[0].template,
            "REPO": str(ROOT),
            "GOLDEN_PAGE": golden_page,
            "GOLDEN_CODE": golden_code,
            "ASSIGNMENTS": assignments,
            "ALLOWLIST": allowlist(rows, batch_id),
            "REQUIREMENTS": requirements(category),
            "VERIFY_COMMANDS": verify_commands(rows),
        },
    )


def render_reviewer(args: argparse.Namespace, pages: list[catalogue.Page]) -> str:
    rows = resolve_pages(args.pages, pages)
    counts = Counter(r.category for r in rows)
    majority = max(catalogue.CATEGORIES, key=lambda c: counts.get(c, 0))
    golden_page, _ = golden_pair(majority)
    lines = []
    for r in rows:
        artifacts = ", ".join(f for f in r.required_files()[1:]) if r.code else "no code"
        lines.append(f"- {r.path}  ({r.id}, {r.category}, batch {r.batch}, tier {r.tier}) - code: {artifacts}")
    return fill(
        "reviewer.md",
        {
            "REVIEWER_ID": args.reviewer,
            "REPO": str(ROOT),
            "GOLDEN_PAGE": golden_page,
            "PAGE_LIST": "\n".join(lines),
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--batch", metavar="ID", help="render the author prompt for every row of this batch")
    mode.add_argument("--ids", metavar="ID,ID", help="render the author prompt for explicit row ids (one category)")
    mode.add_argument("--reviewer", metavar="RID", help="render the reviewer prompt with this reviewer id (needs --pages)")
    parser.add_argument("--only", metavar="SLUG,SLUG", help="with --batch: narrow to these slugs (re-spawn); siblings are listed as read-only context")
    parser.add_argument("--pages", metavar="ID|PATH,...", help="with --reviewer: ids, slugs or docs paths to review")
    parser.add_argument("--out", metavar="FILE", help="write the prompt to FILE instead of stdout")
    args = parser.parse_args(argv)
    if args.only and not args.batch:
        parser.error("--only requires --batch")
    if args.reviewer and not args.pages:
        parser.error("--reviewer requires --pages")
    if args.pages and not args.reviewer:
        parser.error("--pages requires --reviewer")

    pages = catalogue.load()
    errors = catalogue.validate(pages)
    if errors:
        for e in errors:
            print("CATALOGUE ERROR:", e, file=sys.stderr)
        return 1

    text = render_reviewer(args, pages) if args.reviewer else render_author(args, pages)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"render_prompt: wrote {args.out} ({len(text.splitlines())} lines)", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
