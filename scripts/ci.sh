#!/bin/sh
# Local equivalent of .github/workflows/ci.yml (one Python, one Node version).
set -eu
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
"$PYTHON" -m ruff check .
"$PYTHON" -m pytest -q
(cd ts && npm ci --silent && MCPILOT_PYTHON="$(cd .. && pwd)/$PYTHON" npm test)
PYTHON="$PYTHON" scripts/build_artifacts.sh
