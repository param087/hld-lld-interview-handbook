#!/usr/bin/env python3
"""Regenerate the `nav:` block of mkdocs.yml between `# BEGIN-NAV` and `# END-NAV`.

Pure text replacement: mkdocs.yml is never parsed as YAML (it contains `!!python/name:`
tags). Everything outside the two marker lines is preserved byte for byte.

By default only catalogue pages that exist on disk are listed, so intermediate
`mkdocs build --strict` runs do not fail on `nav.not_found`.

Usage: python scripts/gen_nav.py            # rewrite the block (existing pages only)
       python scripts/gen_nav.py --all      # list every catalogue page regardless
       python scripts/gen_nav.py --check    # exit 1 if the block on disk is stale
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import catalogue  # noqa: E402

ROOT = catalogue.ROOT
MKDOCS = ROOT / "mkdocs.yml"
BEGIN = "# BEGIN-NAV"
END = "# END-NAV"

# (top-level label, hub dir or None, [(sub-label, section dir), ...])
STRUCTURE: tuple[tuple[str, str | None, tuple[tuple[str, str], ...]], ...] = (
    ("HLD", "hld", (("Fundamentals", "hld/fundamentals"), ("Case studies", "hld/case-studies"))),
    ("LLD", "lld", (("Fundamentals", "lld/fundamentals"), ("Design patterns", "lld/patterns"), ("Problems", "lld/problems"))),
    ("Cheatsheets", None, (("", "cheatsheets"),)),
    ("Mock interviews", None, (("", "mocks"),)),
)
_SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ()+,./'-]*")
_YAML_WORDS = {"yes", "no", "on", "off", "true", "false", "null", "y", "n", "~"}


def yaml_key(title: str) -> str:
    """Quote a nav title only when YAML would otherwise misread it."""
    if _SAFE_KEY.fullmatch(title) and ": " not in title and " #" not in title and title.lower() not in _YAML_WORDS:
        return title
    return json.dumps(title, ensure_ascii=False)


def _exists(rel: str, include_all: bool) -> bool:
    return include_all or (ROOT / "docs" / rel).is_file()


def _section_entries(pages: list[catalogue.Page], section_dir: str, include_all: bool) -> list[str]:
    """Entries (without indentation) for one section: its index first, then its pages."""
    entries: list[str] = []
    if _exists(f"{section_dir}/index.md", include_all):
        entries.append(f"- {section_dir}/index.md")
    for p in pages:
        if p.section != section_dir:
            continue
        rel = p.path.removeprefix("docs/")
        if _exists(rel, include_all):
            entries.append(f"- {yaml_key(p.title)}: {rel}")
    return entries


def render_nav(pages: list[catalogue.Page], include_all: bool = False) -> str:
    """The text between the markers (no marker lines), ending with a newline."""
    lines = ["nav:", "  - Home: index.md"]
    if _exists("roadmap.md", include_all):
        lines.append("  - Roadmap: roadmap.md")
    for label, hub_dir, subsections in STRUCTURE:
        block: list[str] = []
        if hub_dir and _exists(f"{hub_dir}/index.md", include_all):
            block.append(f"      - {hub_dir}/index.md")
        for sub_label, section_dir in subsections:
            entries = _section_entries(pages, section_dir, include_all)
            if not entries:
                continue
            if sub_label:
                block.append(f"      - {sub_label}:")
                block += [f"          {e}" for e in entries]
            else:
                block += [f"      {e}" for e in entries]
        if block:
            lines.append(f"  - {label}:")
            lines += block
    return "\n".join(lines) + "\n"


def split_mkdocs(text: str) -> tuple[str, str, str]:
    """(head including the BEGIN line, current block, tail starting at the END line)."""
    begin_at = text.find(BEGIN)
    end_at = text.find(END)
    if begin_at < 0 or end_at < 0 or end_at < begin_at:
        raise SystemExit(f"gen_nav: {MKDOCS.name} must contain '{BEGIN}' before '{END}'")
    head_end = text.index("\n", begin_at) + 1
    return text[:head_end], text[head_end:end_at], text[end_at:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="include every catalogue page even if its file is missing")
    parser.add_argument("--check", action="store_true", help="do not write; exit 1 if the nav block on disk differs")
    args = parser.parse_args(argv)

    pages = catalogue.load()
    errors = catalogue.validate(pages)
    if errors:
        for e in errors:
            print("CATALOGUE ERROR:", e, file=sys.stderr)
        return 1

    text = MKDOCS.read_text(encoding="utf-8")
    head, current, tail = split_mkdocs(text)
    wanted = render_nav(pages, include_all=args.all)
    listed = wanted.count(".md")

    if args.check:
        if current != wanted:
            print(f"gen_nav --check: nav block in {MKDOCS.name} is out of date (run scripts/gen_nav.py{' --all' if args.all else ''})")
            return 1
        print(f"gen_nav --check: nav block up to date ({listed} entries)")
        return 0

    if current == wanted:
        print(f"gen_nav: nav block unchanged ({listed} entries)")
        return 0
    MKDOCS.write_text(head + wanted + tail, encoding="utf-8")
    print(f"gen_nav: rewrote nav block ({listed} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
