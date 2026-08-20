#!/usr/bin/env python3
"""Parse CATALOGUE.md into Page records. Every other script imports this.

Usage: python scripts/catalogue.py            # validate + print summary
       python scripts/catalogue.py --json     # dump records as JSON
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "CATALOGUE.md"
CATEGORIES = (
    "hld-fundamental",
    "hld-case-study",
    "lld-fundamental",
    "design-pattern",
    "lld-problem",
    "cheatsheet",
    "mock-interview",
)
COLUMNS = ["id", "slug", "title", "path", "code", "diagrams", "tier", "weight", "batch", "links_to", "scope"]
WORD_TARGETS = {
    "hld-fundamental": (1500, 2500),
    "hld-case-study": (2200, 3200),
    "lld-fundamental": (1200, 2000),
    "design-pattern": (800, 1300),
    "lld-problem": (1800, 2800),
    "cheatsheet": (600, 1500),
    "mock-interview": (2500, 3500),
}
# Minimum Mermaid fences per category (case studies add the standard set of 5).
MIN_DIAGRAMS = {
    "hld-fundamental": 1,
    "hld-case-study": 5,
    "lld-fundamental": 0,
    "design-pattern": 1,
    "lld-problem": 2,
    "cheatsheet": 0,
    "mock-interview": 2,
}


@dataclass
class Page:
    id: str
    slug: str
    title: str
    path: str
    code: list[str]
    diagrams: list[str]
    tier: str
    weight: str
    batch: str
    links_to: list[str]
    scope: str
    category: str
    brief: str = ""
    # derived
    code_paths: list[Path] = field(default_factory=list, repr=False)

    @property
    def template(self) -> str:
        return f"docs/_templates/{self.category}.md"

    @property
    def packages(self) -> list[str]:
        """Package directories (entries ending with '/')."""
        return [c for c in self.code if c.endswith("/")]

    @property
    def modules(self) -> list[str]:
        """Single-file artifacts."""
        return [c for c in self.code if c.endswith(".py")]

    @property
    def section(self) -> str:
        """Directory of the page relative to docs/ (e.g. 'hld/case-studies')."""
        return str(Path(self.path).relative_to("docs").parent).replace("\\", "/")

    @property
    def mermaid_min(self) -> int:
        return MIN_DIAGRAMS[self.category]

    @property
    def word_range(self) -> tuple[int, int]:
        return WORD_TARGETS[self.category]

    def required_files(self) -> list[str]:
        """Every file this row obliges an author to create."""
        files = [self.path]
        for pkg in self.packages:
            name = Path(pkg.rstrip("/")).name
            files += [
                f"{pkg}__init__.py",
                f"{pkg}models.py",
                f"{pkg}services.py",
                f"{pkg}demo.py",
                f"{pkg}tests/__init__.py",
                f"{pkg}tests/test_{name}.py",
            ]
        for mod in self.modules:
            p = Path(mod)
            files += [mod, str(p.parent / "tests" / f"test_{p.stem}.py")]
        return files

    def demo_modules(self) -> list[str]:
        mods = []
        for pkg in self.packages:
            parts = Path(pkg.rstrip("/")).parts  # ('code', 'lld', 'parking_lot')
            mods.append(".".join(parts[1:]) + ".demo")
        for mod in self.modules:
            parts = Path(mod).with_suffix("").parts
            mods.append(".".join(parts[1:]))
        return mods


def _split_row(line: str) -> list[str]:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def _csv(cell: str) -> list[str]:
    cell = cell.strip()
    if cell in ("", "-"):
        return []
    return [c.strip() for c in cell.split(",") if c.strip()]


def load(path: Path = CATALOGUE) -> list[Page]:
    pages: list[Page] = []
    briefs: dict[str, list[str]] = {}
    category: str | None = None
    in_briefs = False
    brief_id: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            heading = line[3:].strip()
            in_briefs = heading.lower() == "briefs"
            category = heading if heading in CATEGORIES else None
            brief_id = None
            continue
        if in_briefs:
            if line.startswith("### "):
                brief_id = line[4:].strip()
                briefs[brief_id] = []
            elif brief_id and line.strip():
                briefs[brief_id].append(line.strip())
            continue
        if category is None or not line.startswith("|"):
            continue
        cells = _split_row(line)
        if cells[0] == "id" or set(cells[0]) <= {"-", ":"}:
            continue
        if len(cells) != len(COLUMNS):
            raise ValueError(f"CATALOGUE.md: row has {len(cells)} cells, expected {len(COLUMNS)}: {line[:80]}")
        rec = dict(zip(COLUMNS, cells, strict=True))
        page = Page(
            id=rec["id"],
            slug=rec["slug"],
            title=rec["title"],
            path=rec["path"],
            code=_csv(rec["code"]),
            diagrams=_csv(rec["diagrams"]),
            tier=rec["tier"],
            weight=rec["weight"],
            batch=rec["batch"],
            links_to=_csv(rec["links_to"]),
            scope=rec["scope"],
            category=category,
        )
        page.code_paths = [ROOT / c for c in page.code]
        pages.append(page)
    for page in pages:
        if page.id in briefs:
            page.brief = "\n".join(briefs[page.id])
    return pages


def validate(pages: list[Page]) -> list[str]:
    errors: list[str] = []
    for col in ("id", "slug", "path"):
        dupes = [k for k, n in Counter(getattr(p, col) for p in pages).items() if n > 1]
        if dupes:
            errors.append(f"duplicate {col}: {dupes}")
    code_dupes = [k for k, n in Counter(c for p in pages for c in p.code).items() if n > 1]
    if code_dupes:
        errors.append(f"code artifact owned by more than one page: {code_dupes}")
    slugs = {p.slug for p in pages}
    for p in pages:
        if not re.fullmatch(r"[A-Z]{2}-\d{2}", p.id):
            errors.append(f"{p.id}: bad id format")
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", p.slug):
            errors.append(f"{p.id}: bad slug {p.slug!r}")
        if not p.path.startswith("docs/") or not p.path.endswith(f"/{p.slug}.md"):
            errors.append(f"{p.id}: path {p.path!r} must be docs/<section>/{p.slug}.md")
        if p.tier not in ("P0", "P1", "P2"):
            errors.append(f"{p.id}: bad tier {p.tier}")
        if p.weight not in ("S", "M", "L"):
            errors.append(f"{p.id}: bad weight {p.weight}")
        for link in p.links_to:
            if link not in slugs:
                errors.append(f"{p.id}: links_to unknown slug {link!r}")
        for c in p.code:
            if not c.startswith("code/") or not (c.endswith("/") or c.endswith(".py")):
                errors.append(f"{p.id}: bad code artifact {c!r}")
        if p.category == "lld-problem" and not p.brief:
            errors.append(f"{p.id}: lld-problem rows need a brief under ## Briefs")
    return errors


def by_id(pages: list[Page]) -> dict[str, Page]:
    return {p.id: p for p in pages}


def by_slug(pages: list[Page]) -> dict[str, Page]:
    return {p.slug: p for p in pages}


def by_batch(pages: list[Page], batch: str) -> list[Page]:
    return [p for p in pages if p.batch == batch]


def main(argv: list[str]) -> int:
    pages = load()
    errors = validate(pages)
    if "--json" in argv:
        print(json.dumps([asdict(p) | {"code_paths": [str(c) for c in p.code_paths]} for p in pages], indent=1))
        return 1 if errors else 0
    cats = Counter(p.category for p in pages)
    batches = Counter(p.batch for p in pages)
    print(f"{len(pages)} pages")
    for c in CATEGORIES:
        print(f"  {c:16s} {cats.get(c, 0):3d}")
    print("batches:", ", ".join(f"{b}={n}" for b, n in sorted(batches.items())))
    for e in errors:
        print("ERROR:", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
