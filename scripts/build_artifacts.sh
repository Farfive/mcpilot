#!/bin/sh
# Versioned pilot artifacts: Python wheel + sdist, TypeScript tarball, SHA256SUMS in dist/.
set -eu
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"
rm -rf dist
"$PYTHON" -m build --outdir dist
if tar tzf dist/*.tar.gz | grep -qE "node_modules|/\.venv/"; then
  echo "sdist contains node_modules or a virtualenv" >&2
  exit 1
fi
(cd ts && npm ci --silent && npm pack --silent --pack-destination ../dist >/dev/null)
if command -v sha256sum >/dev/null 2>&1; then SUM="sha256sum"; else SUM="shasum -a 256"; fi
(cd dist && $SUM * > SHA256SUMS)
cat dist/SHA256SUMS
