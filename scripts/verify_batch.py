#!/usr/bin/env python3
"""Orchestrator helper: mechanically verify one authoring batch after its agent returns.

Usage: python scripts/verify_batch.py <BATCH-ID> [--no-mermaid]
Runs ruff, pytest, demos, the page linter (planned mode), the Mermaid validator and an
ownership check (files changed in the working tree that no batch owns). Exit 1 on problems.
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
RUFF = str(ROOT / ".venv" / "bin" / "ruff")


def run(cmd: list[str], tail: int = 6) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, "\n".join(out[-tail:])


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    batch = argv[0]
    no_mermaid = "--no-mermaid" in argv
    pages = catalogue.load()
    rows = catalogue.by_batch(pages, batch)
    if not rows:
        print(f"unknown batch {batch}")
        return 2
    page_paths = [r.path for r in rows]
    tests = [f for r in rows for f in r.required_files() if "/tests/test_" in f]
    code = sorted({c.rstrip("/") for r in rows for c in r.code})
    demos = [m for r in rows for m in r.demo_modules()]
    fail = False
    print(f"== batch {batch}: {len(rows)} pages, {len(code)} code artifacts ==")

    missing = [f for f in page_paths + tests if not (ROOT / f).exists()]
    for f in missing:
        print("MISSING:", f)
    fail |= bool(missing)

    if code:
        rc, out = run([RUFF, "check", *code, *[t for t in tests if (ROOT / t).exists()]], tail=4)
        print("-- ruff --\n" + out)
        fail |= rc != 0
        existing_tests = [t for t in tests if (ROOT / t).exists()]
        if existing_tests:
            rc, out = run([PY, "-m", "pytest", "-q", *existing_tests], tail=3)
            print("-- pytest --\n" + out)
            fail |= rc != 0
        print("-- demos --")
        for m in demos:
            rc, out = run([PY, "-m", m], tail=3)
            lines = len(out.splitlines())
            print(f"{'ok  ' if rc == 0 else 'FAIL'} {m} ({lines} lines)" + ("" if rc == 0 else "\n" + out))
            fail |= rc != 0

    existing_pages = [p for p in page_paths if (ROOT / p).exists()]
    if existing_pages:
        rc, out = run([PY, "scripts/lint_pages.py", "--planned", *existing_pages], tail=10)
        print("-- lint (planned) --\n" + out)
        fail |= rc != 0
        if not no_mermaid:
            rc, out = run(["node", "scripts/validate_mermaid.mjs", "--files", *existing_pages], tail=5)
            print("-- mermaid --\n" + out)
            fail |= rc != 0
        print("-- words / diagrams / snippets --")
        for p in existing_pages:
            t = (ROOT / p).read_text(encoding="utf-8")
            body = re.sub(r"^---.*?---\n", "", t, flags=re.S)
            body = re.sub(r"```.*?```", "", body, flags=re.S)
            words = len(re.findall(r"\b\w+\b", body))
            lo, hi = next(r.word_range for r in rows if r.path == p)
            flag = "" if lo <= words <= hi else f"  <-- outside {lo}-{hi}"
            print(f"{words:5d} words  {t.count('```mermaid'):2d} diagrams  {t.count('--8<--'):2d} snippets  {p}{flag}")

    # ownership: anything changed in the tree that no catalogue row owns?
    owned: dict[str, str] = {}
    for r in pages:
        for f in r.required_files():
            owned[f] = r.batch
        for c in r.packages:
            owned[c] = r.batch
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, capture_output=True, text=True
    ).stdout.splitlines()
    changed = [line[3:] for line in status]

    def owner(path: str) -> str | None:
        if path in owned:
            return owned[path]
        for prefix, b in owned.items():
            if prefix.endswith("/") and path.startswith(prefix):
                return b
        return None

    mine = [f for f in changed if owner(f) == batch]
    others = [f for f in changed if owner(f) not in (None, batch)]
    stray = [f for f in changed if owner(f) is None]
    print(f"-- ownership: {len(changed)} changed files | this batch {len(mine)} | other batches {len(others)} | unowned {len(stray)} --")
    for f in stray:
        print("  UNOWNED:", f)
    print(f"== {batch}: {'VERIFIED' if not fail else 'PROBLEMS'} ==")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
