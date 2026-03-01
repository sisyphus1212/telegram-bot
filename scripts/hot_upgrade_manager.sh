#!/usr/bin/env bash
set -euo pipefail

# Stable manager hot-upgrade path:
# 1) fetch latest code with timeout/no-prompt
# 2) fast-forward only (no implicit merge)
# 3) pre-restart syntax check
# 4) restart service (manager handles drain on SIGTERM)
# 5) health check + short logs

REPO_DIR="${REPO_DIR:-/root/telegram-bot}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-agent-manager}"
GIT_TIMEOUT_S="${GIT_TIMEOUT_S:-60}"
FORCE="${FORCE:-0}"

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "ERR: not a git repo: $REPO_DIR" >&2
  exit 2
fi

if [[ -f /etc/agent-manager.env ]]; then
  # shellcheck disable=SC1091
  source /etc/agent-manager.env || true
fi

export GIT_TERMINAL_PROMPT=0
cd "$REPO_DIR"

before_sha="$(git rev-parse --short HEAD)"
echo "[upgrade] repo=$REPO_DIR branch=$BRANCH before=$before_sha"

fetch_ok=0
for i in 1 2; do
  if timeout "$GIT_TIMEOUT_S" git fetch origin "$BRANCH"; then
    fetch_ok=1
    break
  fi
  echo "[upgrade] WARN: git fetch attempt $i failed (timeout/network), retrying..." >&2
  sleep 1
done

if [[ "$fetch_ok" -ne 1 ]]; then
  if ! git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1; then
    echo "[upgrade] ERR: fetch failed and missing origin/$BRANCH ref" >&2
    exit 3
  fi
  echo "[upgrade] WARN: fetch failed; continue with existing origin/$BRANCH ref" >&2
fi

git merge --ff-only "origin/$BRANCH"
after_sha="$(git rev-parse --short HEAD)"
echo "[upgrade] after=$after_sha"

if [[ -x .venv/bin/python ]]; then
  .venv/bin/python -m py_compile manager/entry/app.py
fi

if [[ "$FORCE" != "1" ]] && [[ -n "${CODEX_MANAGER_CONTROL_LISTEN:-}" ]] && [[ -n "${CODEX_MANAGER_CONTROL_TOKEN:-}" ]] && [[ -x .venv/bin/python ]]; then
  status_json="$(
    CODEX_CTL_LISTEN="$CODEX_MANAGER_CONTROL_LISTEN" CODEX_CTL_TOKEN="$CODEX_MANAGER_CONTROL_TOKEN" .venv/bin/python - <<'PY' 2>/dev/null || true
import json, os, socket
listen = os.environ.get("CODEX_CTL_LISTEN", "")
token = os.environ.get("CODEX_CTL_TOKEN", "")
if ":" not in listen or not token:
    raise SystemExit(0)
host, port_s = listen.rsplit(":", 1)
port = int(port_s)
req = {"type": "status", "token": token}
s = socket.create_connection((host, port), timeout=2.0)
s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
buf = b""
while not buf.endswith(b"\n"):
    part = s.recv(4096)
    if not part:
        break
    buf += part
s.close()
print(buf.decode("utf-8", "replace").strip())
PY
  )"
  inflight="$(printf '%s' "$status_json" | sed -n 's/.*"inflight_tasks":[[:space:]]*\([0-9]\+\).*/\1/p' | head -n1)"
  if [[ -n "$inflight" ]] && (( inflight > 0 )); then
    echo "[upgrade] ABORT: inflight_tasks=$inflight (set FORCE=1 to override)" >&2
    exit 4
  fi
fi

systemctl restart "$SERVICE_NAME"
sleep 2
systemctl is-active "$SERVICE_NAME" >/dev/null
echo "[upgrade] service=$SERVICE_NAME active"

journalctl -u "$SERVICE_NAME" -n 20 --no-pager
