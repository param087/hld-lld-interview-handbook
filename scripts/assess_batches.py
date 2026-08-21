#!/usr/bin/env python3
"""Per-slug completion state for authoring batches (page exists + lint clean + tests pass).

Usage: python scripts/assess_batches.py BATCH [BATCH...] [--files] [--todo]
  --files  print the required files of every COMPLETE slug (for `git add`)
  --todo   print `BATCH slug1,slug2` lines for the unfinished slugs (for render_prompt --only)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import catalogue  # noqa: E402

ROOT = catalogue.ROOT
PY = str(ROOT / ".venv" / "bin" / "python")


def lint(path: str) -> tuple[bool, str]:
    r = subprocess.run([PY, "scripts/lint_pages.py", "--planned", path], capture_output=True, text=True, cwd=ROOT)
    m = re.search(r"(\d+) errors, (\d+) warnings", r.stdout)
    return r.returncode == 0, (m.group(0) if m else r.stdout.strip()[-80:])


def tests(files: list[str]) -> tuple[bool, str]:
    existing = [t for t in files if (ROOT / t).exists()]
    if len(existing) < len(files):
        return False, f"{len(existing)}/{len(files)} test files"
    if not existing:
        return True, "no code"
    r = subprocess.run([PY, "-m", "pytest", "-q", *existing], capture_output=True, text=True, cwd=ROOT)
    last = (r.stdout.strip().splitlines() or ["?"])[-1]
    return r.returncode == 0, last[:70]


def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    batches = [a for a in argv if not a.startswith("--")]
    pages = catalogue.load()
    complete_files: list[str] = []
    todo_lines: list[str] = []
    for b in batches:
        rows = catalogue.by_batch(pages, b)
        done, todo = [], []
        if not flags:
            print(f"\n== {b} ==")
        for r in rows:
            page_ok = (ROOT / r.path).exists()
            req = r.required_files()
            code_ok = all((ROOT / f).exists() for f in req if f.startswith("code/"))
            lint_ok, lint_msg = lint(r.path) if page_ok else (False, "no page")
            t_ok, t_msg = tests([f for f in req if "/tests/test_" in f])
            ok = page_ok and code_ok and lint_ok and t_ok
            (done if ok else todo).append(r.slug)
            if ok:
                complete_files += req
            if not flags:
                print(f"  {'OK  ' if ok else 'TODO'} {r.id} {r.slug:42s} page={'y' if page_ok else 'n'} code={'y' if code_ok else 'n'} lint={lint_msg} tests={t_msg}")
        if todo:
            todo_lines.append(f"{b} {','.join(todo)}")
        if not flags:
            print(f"  -> done {len(done)}/{len(rows)}")
    if "--files" in flags:
        print("\n".join(f for f in complete_files if (ROOT / f).exists()))
    if "--todo" in flags:
        print("\n".join(todo_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
