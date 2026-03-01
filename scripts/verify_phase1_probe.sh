#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PROMPT="ping"
REPEAT=10
TIMEOUT=60
INTERVAL_MS=0
JSONL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt)
      PROMPT="${2:-}"; shift 2;;
    --repeat)
      REPEAT="${2:-}"; shift 2;;
    --timeout)
      TIMEOUT="${2:-}"; shift 2;;
    --interval-ms)
      INTERVAL_MS="${2:-}"; shift 2;;
    --jsonl)
      JSONL="${2:-}"; shift 2;;
    -h|--help)
      cat <<EOF
Usage: scripts/verify_phase1_probe.sh [--prompt ping] [--repeat 10] [--timeout 60] [--interval-ms 0] [--jsonl log/probe_phase1.jsonl]
EOF
      exit 0;;
    *)
      echo "unknown arg: $1" >&2
      exit 2;;
  esac
done

if [[ ! -x ".venv/bin/python" ]]; then
  echo "missing venv: run ./scripts/install.sh first" >&2
  exit 2
fi

. .venv/bin/activate

echo "phase=1 prompt=${PROMPT@Q} repeat=$REPEAT timeout=$TIMEOUT interval_ms=$INTERVAL_MS jsonl=${JSONL@Q}"

set +e
python codex_node_probe.py \
  --prompt "$PROMPT" \
  --repeat "$REPEAT" \
  --timeout "$TIMEOUT" \
  --interval-ms "$INTERVAL_MS" \
  ${JSONL:+--jsonl "$JSONL"}
rc=$?
set -e

if [[ $rc -ne 0 ]]; then
  echo "phase=1 FAIL rc=$rc" >&2
  if [[ -n "$JSONL" && -f "$JSONL" ]]; then
    echo "last jsonl lines:" >&2
    tail -n 20 "$JSONL" >&2 || true
  fi
  exit $rc
fi

echo "phase=1 PASS"

