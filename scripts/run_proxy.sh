#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing venv. Run: scripts/install.sh" >&2
  exit 1
fi

exec .venv/bin/python codex_proxy.py
