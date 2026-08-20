#!/usr/bin/env bash
# One-time local setup: venv with Python 3.12, deps, and `code/` on sys.path so that
# `uv run python -m lld.parking_lot.demo` works without PYTHONPATH.
set -euo pipefail
cd "$(dirname "$0")/.."
UV="${UV:-$HOME/.local/bin/uv}"
[ -d .venv ] || "$UV" venv --python 3.12 .venv
"$UV" pip install -q -r requirements-dev.txt
SITE="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
printf '%s\n' "$(pwd)/code" > "$SITE/handbook-code.pth"
echo "ok: $(.venv/bin/python --version); code/ on sys.path via $SITE/handbook-code.pth"
