#!/usr/bin/env bash
set -euo pipefail

CONTROL_HOSTPORT="${CODEX_MANAGER_CONTROL:-127.0.0.1:18766}"
CONTROL_TOKEN="${CODEX_MANAGER_CONTROL_TOKEN:-}"
TIMEOUT="${CODEX_MANAGER_CONTROL_TIMEOUT:-30}"

usage() {
  cat <<'USAGE'
Usage:
  CODEX_MANAGER_CONTROL_TOKEN=... scripts/manager_ctl.sh <command> [options]

Commands:
  status
  servers
  dispatch --node-id <id> [--prompt <text>] [--timeout <sec>] [--model <id>] [--effort <low|medium|high>]
  appserver --node-id <id> --method <name> [--params-json '<json>'] [--timeout <sec>]
  dispatch-text --node-id <id> --session-key <key> --text <text> [--timeout <sec>]
  token-generate --node-id <id> [--note <text>] [--created-by <tg_user_id>] [--print-config]
  token-list [--include-revoked]
  token-revoke --token-id <id> [--revoked-by <tg_user_id>]

Env:
  CODEX_MANAGER_CONTROL=127.0.0.1:18766
  CODEX_MANAGER_CONTROL_TOKEN=...
  CODEX_MANAGER_CONTROL_TIMEOUT=30
USAGE
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi
if [[ -z "$CONTROL_TOKEN" ]]; then
  echo "missing CODEX_MANAGER_CONTROL_TOKEN" >&2
  exit 2
fi

CMD="$1"
shift || true

NODE_ID=""
PROMPT="ping"
METHOD=""
PARAMS_JSON="{}"
SESSION_KEY=""
TEXT=""
NOTE=""
TOKEN_ID=""
MODEL=""
EFFORT=""
CREATED_BY=""
REVOKED_BY=""
INCLUDE_REVOKED="0"
PRINT_CONFIG="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --node-id) NODE_ID="${2:-}"; shift 2 ;;
    --prompt) PROMPT="${2:-}"; shift 2 ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    --method) METHOD="${2:-}"; shift 2 ;;
    --params-json) PARAMS_JSON="${2:-}"; shift 2 ;;
    --session-key) SESSION_KEY="${2:-}"; shift 2 ;;
    --text) TEXT="${2:-}"; shift 2 ;;
    --note) NOTE="${2:-}"; shift 2 ;;
    --token-id) TOKEN_ID="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --effort) EFFORT="${2:-}"; shift 2 ;;
    --created-by) CREATED_BY="${2:-}"; shift 2 ;;
    --revoked-by) REVOKED_BY="${2:-}"; shift 2 ;;
    --include-revoked) INCLUDE_REVOKED="1"; shift 1 ;;
    --print-config) PRINT_CONFIG="1"; shift 1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

export CONTROL_HOSTPORT CONTROL_TOKEN CMD NODE_ID PROMPT METHOD PARAMS_JSON SESSION_KEY TEXT NOTE TOKEN_ID MODEL EFFORT CREATED_BY REVOKED_BY INCLUDE_REVOKED PRINT_CONFIG TIMEOUT

python3 - <<'PY'
import json
import os
import socket
from typing import Any

host, port_s = os.environ["CONTROL_HOSTPORT"].rsplit(":", 1)
port = int(port_s)
token = os.environ["CONTROL_TOKEN"]
cmd = os.environ["CMD"]

def to_float(v: str, d: float) -> float:
    try:
        return float(v)
    except Exception:
        return d

timeout = to_float(os.environ.get("TIMEOUT", "30"), 30.0)
req: dict[str, Any] = {"token": token}

if cmd == "status":
    req["type"] = "status"
elif cmd == "servers":
    req["type"] = "servers"
elif cmd == "dispatch":
    node_id = os.environ.get("NODE_ID", "").strip()
    if not node_id:
        raise SystemExit("missing --node-id")
    req.update({
        "type": "dispatch",
        "node_id": node_id,
        "prompt": os.environ.get("PROMPT", "ping"),
        "timeout": timeout,
    })
    model = os.environ.get("MODEL", "").strip()
    effort = os.environ.get("EFFORT", "").strip()
    if model:
        req["model"] = model
    if effort:
        req["effort"] = effort
elif cmd == "appserver":
    node_id = os.environ.get("NODE_ID", "").strip()
    method = os.environ.get("METHOD", "").strip()
    if not node_id:
        raise SystemExit("missing --node-id")
    if not method:
        raise SystemExit("missing --method")
    params_raw = os.environ.get("PARAMS_JSON", "{}")
    try:
        params = json.loads(params_raw)
    except Exception as e:
        raise SystemExit(f"--params-json is not valid json: {e}")
    if not isinstance(params, dict):
        raise SystemExit("--params-json must be a JSON object")
    req.update({
        "type": "appserver",
        "node_id": node_id,
        "method": method,
        "params": params,
        "timeout": timeout,
    })
elif cmd == "dispatch-text":
    node_id = os.environ.get("NODE_ID", "").strip()
    session_key = os.environ.get("SESSION_KEY", "").strip()
    text = os.environ.get("TEXT", "")
    if not node_id:
        raise SystemExit("missing --node-id")
    if not session_key:
        raise SystemExit("missing --session-key")
    req.update({
        "type": "dispatch_text",
        "node_id": node_id,
        "session_key": session_key,
        "text": text,
        "timeout": timeout,
    })
elif cmd == "token-generate":
    node_id = os.environ.get("NODE_ID", "").strip()
    note = os.environ.get("NOTE", "").strip()
    if not node_id:
        raise SystemExit("missing --node-id")
    req.update({
        "type": "token_generate",
        "node_id": node_id,
        "note": note,
    })
    cb = os.environ.get("CREATED_BY", "").strip()
    if cb:
        try:
            req["created_by"] = int(cb)
        except Exception:
            raise SystemExit("--created-by must be int")
elif cmd == "token-list":
    req.update({
        "type": "token_list",
        "include_revoked": os.environ.get("INCLUDE_REVOKED", "0") == "1",
    })
elif cmd == "token-revoke":
    token_id = os.environ.get("TOKEN_ID", "").strip()
    if not token_id:
        raise SystemExit("missing --token-id")
    req.update({
        "type": "token_revoke",
        "token_id": token_id,
    })
    rb = os.environ.get("REVOKED_BY", "").strip()
    if rb:
        try:
            req["revoked_by"] = int(rb)
        except Exception:
            raise SystemExit("--revoked-by must be int")
else:
    raise SystemExit(f"unknown command: {cmd}")

with socket.create_connection((host, port), timeout=timeout) as s:
    s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
    fp = s.makefile("r", encoding="utf-8", newline="\n")
    line = fp.readline()
    if not line:
        raise SystemExit("empty response from manager control")
    resp = json.loads(line)

print_config = os.environ.get("PRINT_CONFIG", "0") == "1"
if cmd == "token-generate" and print_config:
    cfg = resp.get("node_config") if isinstance(resp, dict) else None
    if not isinstance(cfg, dict):
        raise SystemExit(json.dumps(resp, ensure_ascii=False, indent=2))
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
else:
    print(json.dumps(resp, ensure_ascii=False, indent=2))
PY
