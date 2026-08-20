#!/usr/bin/env python3
"""Generate docs/roadmap.md (8-week plan + 1-week crash plan) from CATALOGUE.md."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import catalogue  # noqa: E402

ROOT = catalogue.ROOT
OUT = ROOT / "docs" / "roadmap.md"

WEEKS = [
    ("Foundations", "HF-01 HF-02 HF-03 HF-04 HF-05 HF-06", "HC-01", "LF-01 LF-05 DP-00 DP-21 DP-02 DP-01", "LP-01", "",
     "Write the 45-minute HLD framework from memory; run the parking-lot demo and read its tests."),
    ("Data layer", "HF-07 HF-08 HF-09 HF-10 HF-11", "HC-02 HC-03 HC-04", "LF-02 LF-04 DP-20 DP-19 DP-04 DP-09", "LP-03 LP-07 LP-05", "",
     "Produce an estimation sheet for three systems; implement an LRU cache from scratch without looking."),
    ("Correctness at scale", "HF-12 HF-13 HF-14", "HC-05 HC-06 HC-11", "LF-03 LF-06 DP-14 DP-22 DP-10 DP-06 DP-26 DP-24", "LP-02 LP-11 LP-06", "MK-01",
     "Redo the news-feed mock timed; write a concurrency test for the elevator controller."),
    ("Coordination and real-time", "HF-15 HF-16 HF-18 HF-19", "HC-08 HC-14 HC-15 HC-12", "DP-13 DP-17 DP-08 DP-16 DP-12", "LP-12 LP-04 LP-17 LP-18", "MK-02",
     "Redo the chat mock timed; implement a seat hold with TTL and version check."),
    ("Architecture and money", "HF-20 HF-21 HF-22", "HC-07 HC-13 HC-09 HC-16", "DP-18 DP-25 DP-27 DP-31 DP-30", "LP-14 LP-13 LP-26 LP-35", "MK-03",
     "Redo the Ticketmaster mock timed; write a double-entry ledger with idempotency keys."),
    ("Pipelines and analytics", "HF-23 HF-24 HF-25 HF-17", "HC-17 HC-18 HC-19 HC-20 HC-21 HC-22", "DP-03 DP-28 DP-29 DP-23", "LP-29 LP-15 LP-19 LP-20 LP-24", "MK-05",
     "Redo the elevator mock timed; implement a windowed aggregator with a watermark."),
    ("Breadth", "HF-27 HF-26", "HC-23 HC-25 HC-24 HC-26 HC-28 HC-27", "DP-05 DP-07 DP-11 DP-15", "LP-08 LP-09 LP-21 LP-22 LP-23 LP-25 LP-27", "MK-04 MK-06",
     "Redo the parking-lot and movie-booking mocks timed."),
    ("Polish", "", "HC-10 HC-29 HC-30 HC-31", "", "LP-28 LP-30 LP-31 LP-32 LP-33 LP-34 LP-10 LP-16 LP-36", "",
     "Re-read every P0 page and all cheatsheets; two full timed mocks per track with a peer; review the common-mistakes sheet."),
]

CRASH = [
    ("Day 1", "HLD core", "HF-01 HF-02 HF-03 HF-07 HF-08 HF-11 CS-01 CS-02 CS-06"),
    ("Day 2", "HLD correctness + P0 case studies", "HF-10 HF-12 HF-13 HF-14 HF-18 HF-19 HC-01 HC-02 HC-03 HC-04"),
    ("Day 3", "LLD core", "LF-01 LF-02 LF-05 LF-06 DP-21 DP-20 DP-19 DP-02 DP-01 DP-14 DP-04 DP-09 LP-01 LP-03"),
    ("Day 4", "P0 case studies", "HC-05 HC-06 HC-07 HC-08 HC-09 HF-04 HF-24"),
    ("Day 5", "P0 problems", "LP-02 LP-11 LP-12 LP-05 LP-07 LP-06 CS-05 CS-07"),
    ("Day 6", "Mocks (read, then redo timed from the prompt alone)", "MK-01 MK-03 MK-04 MK-06"),
    ("Day 7", "Review", "CS-08 CS-09 CS-04 CS-03 CS-10"),
]


def main() -> int:
    pages = catalogue.by_id(catalogue.load())

    def links(ids: str) -> str:
        out = []
        for i in ids.split():
            p = pages[i]
            rel = Path(p.path).relative_to("docs").as_posix()
            out.append(f"[{p.title}]({rel})")
        return ", ".join(out)

    lines = [
        "---",
        "title: Study roadmap",
        "description: An 8-week plan and a 1-week crash plan that order every page of the handbook for an SDE2 candidate.",
        "---",
        "# Study roadmap",
        "",
        "Two ways through the handbook. The **8-week plan** (10–12 hours a week) covers everything, P0 pages first. "
        "The **1-week crash plan** (6–8 hours a day) is for an interview next week: only P0 material and the mocks.",
        "",
        "Each week ends with a deliverable — something you write or run without looking at the page. "
        "That is the difference between having read a design and being able to produce it in 45 minutes.",
        "",
        "## 8-week plan",
        "",
        "| Week | Theme | Focus |",
        "|---|---|---|",
    ]
    for n, (theme, hf, hc, lf, lp, mk, _deliverable) in enumerate(WEEKS, 1):
        def count(ids: str, singular: str, plural: str) -> str:
            n_items = len(ids.split())
            return f"{n_items} {singular if n_items == 1 else plural}" if n_items else ""

        focus = ", ".join(x for x in [
            count(hf, "HLD fundamental", "HLD fundamentals"),
            count(hc, "case study", "case studies"),
            count(lf, "LLD fundamental/pattern", "LLD fundamentals/patterns"),
            count(lp, "LLD problem", "LLD problems"),
            count(mk, "mock", "mocks"),
        ] if x)
        lines.append(f"| {n} | [{theme}](#week-{n}-{theme.lower().replace(' ', '-')}) | {focus} |")
    lines.append("")
    for n, (theme, hf, hc, lf, lp, mk, deliverable) in enumerate(WEEKS, 1):
        lines += [f"### Week {n}: {theme}", ""]
        if hf:
            lines.append(f"- **HLD fundamentals:** {links(hf)}")
        if hc:
            lines.append(f"- **Case studies:** {links(hc)}")
        if lf:
            lines.append(f"- **LLD fundamentals and patterns:** {links(lf)}")
        if lp:
            lines.append(f"- **LLD problems:** {links(lp)}")
        if mk:
            lines.append(f"- **Mock interview:** {links(mk)}")
        if n == 8:
            lines.append("- **Cheatsheets:** all of them — see the [cheatsheets index](cheatsheets/index.md)")
        lines += [f"- **Deliverable:** {deliverable}", ""]
    lines += [
        "## 1-week crash plan",
        "",
        "| Day | Focus | Pages |",
        "|---|---|---|",
    ]
    for day, focus, ids in CRASH:
        lines.append(f"| {day} | {focus} | {links(ids)} |")
    lines += [
        "",
        "On day 7, also redraw five architectures from memory (news feed, chat, Ticketmaster, key-value store, Uber) and compare them with the pages.",
        "",
        "## How to study a page",
        "",
        "1. Read the TL;DR and the diagrams first; try to predict the deep dives before reading them.",
        "2. For case studies, redo the estimation on paper. For LLD problems, write the class diagram before looking at the implementation, then run the tests.",
        "3. Answer the follow-up questions out loud. If you cannot, that is the section to reread tomorrow.",
        "4. Log the page in your own one-line-per-page notebook: the crux, the numbers, the pattern.",
        "",
        "## Related",
        "",
        "- [The 45-minute HLD framework](hld/fundamentals/interview-framework.md)",
        "- [The LLD interview framework](lld/fundamentals/lld-interview-framework.md)",
        "- [HLD round checklist](cheatsheets/hld-checklist.md) and [LLD round checklist](cheatsheets/lld-checklist.md)",
        "- [Common SDE2 mistakes in design rounds](cheatsheets/common-mistakes-sde2.md)",
    ]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
