#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x ".venv/bin/python" ]]; then
  echo "missing venv: run ./scripts/install.sh first" >&2
  exit 2
fi

exec .venv/bin/python codex_node.py
