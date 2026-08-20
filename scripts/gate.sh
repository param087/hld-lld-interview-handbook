#!/usr/bin/env bash
# Release gate: every check the handbook must pass before merge/deploy, in order.
# Stops at the first failure unless --keep-going is given. Runs from the repo root.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

usage() {
  cat <<'EOF'
Usage: scripts/gate.sh [--quick] [--keep-going] [--help]

Runs, in order (each step prints a banner; the first failure stops the run and exits 1):
  1. .venv/bin/ruff check code scripts
  2. .venv/bin/python -m pytest -q
  3. .venv/bin/python scripts/lint_pages.py --final docs
  4. .venv/bin/python scripts/gen_indexes.py --check
  5. .venv/bin/python scripts/gen_nav.py --check
  6. .venv/bin/python -m mkdocs build --strict -d site
  7. node scripts/validate_mermaid.mjs --site site        (skipped with --quick)
  8. .venv/bin/python scripts/check_completeness.py        (skipped with --quick)

Options:
  --quick       skip the slow steps 7 and 8
  --keep-going  run every step even after a failure (exit code is still 1 if any failed)
  -h, --help    show this help and exit
EOF
}

QUICK=0
KEEP_GOING=0
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    --keep-going) KEEP_GOING=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "gate.sh: unknown option '$arg'" >&2; usage >&2; exit 2 ;;
  esac
done

TOTAL=8
PASSED=0
FAILED=0
SKIPPED=0
FAILED_NAMES=""
START=$(date +%s)

summary() {
  local elapsed=$(( $(date +%s) - START ))
  echo
  echo "================================================================================"
  if [ "$FAILED" -eq 0 ]; then
    echo "GATE PASSED: ${PASSED} passed, ${SKIPPED} skipped, 0 failed (${elapsed}s)"
  else
    echo "GATE FAILED: ${PASSED} passed, ${SKIPPED} skipped, ${FAILED} failed [${FAILED_NAMES# }] (${elapsed}s)"
  fi
}

step() {
  # step <n> <name> <command string>
  local n="$1" name="$2" cmd="$3"
  echo
  echo "================================================================================"
  echo "[${n}/${TOTAL}] ${name}"
  echo "\$ ${cmd}"
  echo "--------------------------------------------------------------------------------"
  if bash -c "$cmd"; then
    echo "[${n}/${TOTAL}] PASS: ${name}"
    PASSED=$((PASSED + 1))
  else
    local rc=$?
    echo "[${n}/${TOTAL}] FAIL (exit ${rc}): ${name}"
    FAILED=$((FAILED + 1))
    FAILED_NAMES="${FAILED_NAMES} ${name}"
    if [ "$KEEP_GOING" -eq 0 ]; then
      summary
      exit 1
    fi
  fi
}

skip() {
  local n="$1" name="$2"
  echo
  echo "================================================================================"
  echo "[${n}/${TOTAL}] SKIP (--quick): ${name}"
  SKIPPED=$((SKIPPED + 1))
}

step 1 "ruff" ".venv/bin/ruff check code scripts"
step 2 "pytest" ".venv/bin/python -m pytest -q"
step 3 "lint_pages --final" ".venv/bin/python scripts/lint_pages.py --final docs"
step 4 "gen_indexes --check" ".venv/bin/python scripts/gen_indexes.py --check"
step 5 "gen_nav --check" ".venv/bin/python scripts/gen_nav.py --check"
step 6 "mkdocs build --strict" ".venv/bin/python -m mkdocs build --strict -d site"
if [ "$QUICK" -eq 1 ]; then
  skip 7 "validate_mermaid --site"
  skip 8 "check_completeness"
else
  step 7 "validate_mermaid --site" "node scripts/validate_mermaid.mjs --site site"
  step 8 "check_completeness" ".venv/bin/python scripts/check_completeness.py"
fi

summary
[ "$FAILED" -eq 0 ]
