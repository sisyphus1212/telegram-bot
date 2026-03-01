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

timeout "$GIT_TIMEOUT_S" git fetch origin "$BRANCH"
git merge --ff-only "origin/$BRANCH"
after_sha="$(git rev-parse --short HEAD)"
echo "[upgrade] after=$after_sha"

if [[ -x .venv/bin/python ]]; then
  .venv/bin/python -m py_compile manager/entry/app.py
fi

systemctl restart "$SERVICE_NAME"
sleep 2
systemctl is-active "$SERVICE_NAME" >/dev/null
echo "[upgrade] service=$SERVICE_NAME active"

journalctl -u "$SERVICE_NAME" -n 20 --no-pager
