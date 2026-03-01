#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG_REL="node_config.json"
PY_BIN=".venv/bin/python"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_REL="${2:-}"
      shift 2
      ;;
    --python)
      PY_BIN="${2:-}"
      shift 2
      ;;
    -h|--help)
      cat <<USAGE
Usage: scripts/run_node_forever.sh [--config node_config.json] [--python .venv/bin/python]
USAGE
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

cd "$PROJECT_DIR"

if [[ ! -x "$PY_BIN" ]]; then
  echo "python not executable: $PY_BIN" >&2
  exit 2
fi
if [[ ! -f "$CONFIG_REL" ]]; then
  echo "config not found: $CONFIG_REL" >&2
  exit 2
fi

LOCK_FILE="/tmp/codex_node_forever.${CONFIG_REL//\//_}.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

while true; do
  "$PY_BIN" codex_node.py --config "$CONFIG_REL" >>/tmp/codex_node_forever.log 2>&1 || true
  sleep 2
done
