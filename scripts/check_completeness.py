#!/usr/bin/env python3
"""Check CATALOGUE.md against the working tree in both directions.

Forward (every catalogue row): the page exists; every required code file exists; the page
is in the mkdocs nav block; it has at least `mermaid_min` ```mermaid fences; its
`## Related` section has at least 3 markdown links.

Reverse: every docs/**/*.md (except index.md files, docs/roadmap.md and docs/_templates/)
is a catalogue path; every package under code/lld/ and every module directly under
code/hld/, code/patterns/, code/fundamentals/ (and their tests/test_*.py) is owned by a row.

Usage: python scripts/check_completeness.py              # gap table; exit 1 if any gap
       python scripts/check_completeness.py --batch X    # gaps of one batch only
       python scripts/check_completeness.py --progress   # progress summary; always exit 0
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import catalogue  # noqa: E402

ROOT = catalogue.ROOT
DOCS = ROOT / "docs"
CODE = ROOT / "code"
MKDOCS = ROOT / "mkdocs.yml"
UNOWNED = "UNOWNED"
MERMAID_FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})\s*\{?\s*\.?mermaid\b", re.MULTILINE)
MD_LINK = re.compile(r"\[[^\]]*\]\([^)\s]+(?:\s+\"[^\"]*\")?\)")
RELATED_H2 = re.compile(r"^## Related\s*$", re.MULTILINE)
NEXT_H2 = re.compile(r"^## ", re.MULTILINE)


@dataclass(frozen=True)
class Gap:
    batch: str
    id: str
    slug: str
    issue: str


# ----------------------------------------------------------------------------- helpers
def nav_pages() -> set[str]:
    """Docs-relative page paths listed in the generated nav block of mkdocs.yml."""
    if not MKDOCS.is_file():
        return set()
    text = MKDOCS.read_text(encoding="utf-8")
    begin, end = text.find("# BEGIN-NAV"), text.find("# END-NAV")
    block = text[begin:end] if 0 <= begin < end else text
    pages: set[str] = set()
    for line in block.splitlines():
        tokens = line.strip().split()
        if tokens and tokens[-1].endswith(".md"):
            pages.add(tokens[-1].strip("'\""))
    return pages


def related_link_count(text: str) -> int:
    """Number of markdown links under `## Related`; -1 when the section is missing."""
    match = RELATED_H2.search(text)
    if not match:
        return -1
    rest = text[match.end() :]
    nxt = NEXT_H2.search(rest)
    section = rest[: nxt.start()] if nxt else rest
    return len(MD_LINK.findall(section))


def check_page(page: catalogue.Page, nav: set[str]) -> list[str]:
    issues: list[str] = []
    path = ROOT / page.path
    if not path.is_file():
        issues.append(f"missing page {page.path}")
    for rel in page.required_files()[1:]:
        if not (ROOT / rel).is_file():
            issues.append(f"missing {rel}")
    if not path.is_file():
        return issues
    if page.path.removeprefix("docs/") not in nav:
        issues.append("not in the mkdocs nav block (run scripts/gen_nav.py)")
    text = path.read_text(encoding="utf-8")
    fences = len(MERMAID_FENCE.findall(text))
    if fences < page.mermaid_min:
        issues.append(f"{fences} mermaid fence(s), catalogue requires >= {page.mermaid_min}")
    links = related_link_count(text)
    if links < 0:
        issues.append("no '## Related' section")
    elif links < 3:
        issues.append(f"'## Related' has {links} link(s), need >= 3")
    return issues


def forward_gaps(pages: list[catalogue.Page]) -> tuple[list[Gap], set[str]]:
    """All per-row gaps, plus the ids of rows with no gap at all."""
    nav = nav_pages()
    gaps: list[Gap] = []
    complete: set[str] = set()
    for page in pages:
        issues = check_page(page, nav)
        if issues:
            gaps += [Gap(page.batch, page.id, page.slug, issue) for issue in issues]
        else:
            complete.add(page.id)
    return gaps, complete


def reverse_gaps(pages: list[catalogue.Page]) -> list[Gap]:
    gaps: list[Gap] = []

    def unowned(issue: str) -> None:
        gaps.append(Gap(UNOWNED, "-", "-", issue))

    owned_docs = {p.path for p in pages}
    if DOCS.is_dir():
        for md in sorted(DOCS.rglob("*.md")):
            rel = md.relative_to(ROOT).as_posix()
            if md.name == "index.md" or rel == "docs/roadmap.md" or rel.startswith("docs/_templates/"):
                continue
            if rel not in owned_docs:
                unowned(f"page not in CATALOGUE.md: {rel}")

    owned_pkgs = {pkg.rstrip("/") for p in pages for pkg in p.packages}
    lld = CODE / "lld"
    if lld.is_dir():
        for entry in sorted(lld.iterdir()):
            if entry.is_dir() and not entry.name.startswith(("_", ".")) and entry.name != "tests":
                rel = entry.relative_to(ROOT).as_posix()
                if rel not in owned_pkgs:
                    unowned(f"package not owned by any row: {rel}/")

    owned_modules = {mod for p in pages for mod in p.modules}
    owned_tests = {f for p in pages for f in p.required_files() if "/tests/test_" in f}
    for sub in ("hld", "patterns", "fundamentals"):
        folder = CODE / sub
        if not folder.is_dir():
            continue
        for py in sorted(folder.glob("*.py")):
            rel = py.relative_to(ROOT).as_posix()
            if py.name != "__init__.py" and rel not in owned_modules:
                unowned(f"module not owned by any row: {rel}")
        for py in sorted((folder / "tests").glob("test_*.py")):
            rel = py.relative_to(ROOT).as_posix()
            if rel not in owned_tests:
                unowned(f"test file not owned by any row: {rel}")
    return gaps


# ----------------------------------------------------------------------------- reports
def print_gap_table(gaps: list[Gap]) -> None:
    if not gaps:
        print("check_completeness: no gaps - catalogue and working tree agree")
        return
    by_batch: dict[str, list[Gap]] = defaultdict(list)
    for gap in gaps:
        by_batch[gap.batch].append(gap)
    id_w = max(len(g.id) for g in gaps)
    slug_w = max(len(g.slug) for g in gaps)
    print(f"Completeness gaps: {len(gaps)} across {len(by_batch)} batch(es)\n")
    for batch in sorted(by_batch, key=lambda b: (b == UNOWNED, b)):
        rows = by_batch[batch]
        pages_hit = len({g.id for g in rows})
        label = "files not owned by any catalogue row" if batch == UNOWNED else f"{pages_hit} page(s) with gaps"
        print(f"[{batch}] {label}")
        for g in rows:
            print(f"  {g.id:<{id_w}}  {g.slug:<{slug_w}}  {g.issue}")
        print()


def print_progress(pages: list[catalogue.Page], complete: set[str]) -> None:
    total_by_batch: Counter[str] = Counter(p.batch for p in pages)
    done_by_batch: Counter[str] = Counter(p.batch for p in pages if p.id in complete)
    total_by_cat: Counter[str] = Counter(p.category for p in pages)
    done_by_cat: Counter[str] = Counter(p.category for p in pages if p.id in complete)
    required = [f for p in pages for f in p.required_files()]
    present = sum(1 for f in required if (ROOT / f).is_file())

    print("Progress by batch (pages complete / total)")
    for batch in sorted(total_by_batch):
        done, total = done_by_batch[batch], total_by_batch[batch]
        bar = "#" * done + "." * (total - done)
        print(f"  {batch:<10} {done:3d}/{total:<3d} {bar}")
    print("\nProgress by category")
    for cat in catalogue.CATEGORIES:
        done, total = done_by_cat[cat], total_by_cat[cat]
        pct = 100.0 * done / total if total else 0.0
        print(f"  {cat:<17} {done:3d}/{total:<3d} {pct:5.1f}%")
    done, total = len(complete), len(pages)
    pct = 100.0 * done / total if total else 0.0
    print(f"\nRequired files present: {present}/{len(required)}")
    print(f"Overall: {done}/{total} pages complete ({pct:.1f}%)")


# ----------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--progress", action="store_true", help="print the progress summary instead of the gap table; always exit 0")
    parser.add_argument("--batch", metavar="ID", help="only report gaps for this batch (plus unowned files)")
    args = parser.parse_args(argv)

    pages = catalogue.load()
    errors = catalogue.validate(pages)
    for e in errors:
        print("CATALOGUE ERROR:", e, file=sys.stderr)

    gaps, complete = forward_gaps(pages)
    gaps += reverse_gaps(pages)

    if args.progress:
        print_progress(pages, complete)
        if errors:
            print(f"\n{len(errors)} catalogue error(s) - see stderr")
        return 0

    if args.batch:
        gaps = [g for g in gaps if g.batch in (args.batch, UNOWNED)]
    print_gap_table(gaps)
    done, total = len(complete), len(pages)
    print(f"Summary: {done}/{total} pages complete, {len(gaps)} gap(s) listed, {len(errors)} catalogue error(s)")
    return 1 if gaps or errors else 0


if __name__ == "__main__":
    sys.exit(main())
