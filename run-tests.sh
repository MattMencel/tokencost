#!/usr/bin/env bash
# Run the TokenCost test suite locally (non-CI).
#
# Uses the repo venv. On first run (or when dev deps change) pass --install to
# pull in pytest/respx:
#
#   ./run-tests.sh --install      # install dev deps, then run
#   ./run-tests.sh                # just run
#   ./run-tests.sh -k calc_cost   # any extra args are forwarded to pytest
#
set -euo pipefail
cd "$(dirname "$0")"

PY=./venv/bin/python
if [[ ! -x "$PY" ]]; then
  echo "error: $PY not found — create the venv first (see onbording.sh)" >&2
  exit 1
fi

if [[ "${1:-}" == "--install" ]]; then
  shift
  "$PY" -m pip install -r requirements-dev.txt
fi

if ! "$PY" -c "import pytest" 2>/dev/null; then
  echo "pytest not installed — run: ./run-tests.sh --install" >&2
  exit 1
fi

exec "$PY" -m pytest tests/ "$@"
