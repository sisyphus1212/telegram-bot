#!/usr/bin/env bash
set -euo pipefail

# Sync proxy env from *current process environment* into /etc/agent-manager.env.
#
# Intended usage (on the target machine, after you login and your shell already has proxy vars):
#   sudo -E /root/telegram-bot/scripts/sync_proxy_to_agent_env.sh
#   sudo systemctl restart agent-manager.service
#
# Notes:
# - systemd services do not read ~/.bashrc, so exporting proxies in your login shell won't
#   affect agent-manager unless you inject them into EnvironmentFile/Environment.
# - We intentionally REMOVE ALL_PROXY/all_proxy to avoid python-telegram-bot selecting SOCKS
#   proxies and requiring extra optional dependencies.

ENV_FILE="${CODEX_AGENT_MANAGER_ENV_FILE:-/etc/agent-manager.env}"
MODE="${CODEX_AGENT_MANAGER_ENV_MODE:-0600}"
KEEP_ALL_PROXY="${CODEX_SYNC_KEEP_ALL_PROXY:-0}"

_pick() {
  # Prefer uppercase, fallback to lowercase.
  local up="$1"
  local low="$2"
  local v="${!up-}"
  if [[ -n "${v:-}" ]]; then
    printf '%s' "$v"
    return 0
  fi
  v="${!low-}"
  if [[ -n "${v:-}" ]]; then
    printf '%s' "$v"
    return 0
  fi
  return 1
}

HTTP_V=""
HTTPS_V=""
NO_V=""
ALL_V=""

HTTP_V="$(_pick HTTP_PROXY http_proxy 2>/dev/null || true)"
HTTPS_V="$(_pick HTTPS_PROXY https_proxy 2>/dev/null || true)"
NO_V="$(_pick NO_PROXY no_proxy 2>/dev/null || true)"
if [[ "$KEEP_ALL_PROXY" == "1" ]]; then
  ALL_V="$(_pick ALL_PROXY all_proxy 2>/dev/null || true)"
fi

if [[ -z "${HTTP_V}${HTTPS_V}${NO_V}" ]]; then
  echo "No proxy vars found in current environment (HTTP_PROXY/HTTPS_PROXY/NO_PROXY)." >&2
  echo "Hint: run with sudo -E from a shell where proxies are exported." >&2
  exit 2
fi

umask 077
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

if [[ -f "$ENV_FILE" ]]; then
  # Remove any existing proxy-related keys we manage.
  # Keep everything else (e.g. TELEGRAM_BOT_TOKEN).
  if [[ "$KEEP_ALL_PROXY" == "1" ]]; then
    sed \
      -e '/^[[:space:]]*HTTP_PROXY=/d' \
      -e '/^[[:space:]]*HTTPS_PROXY=/d' \
      -e '/^[[:space:]]*NO_PROXY=/d' \
      -e '/^[[:space:]]*http_proxy=/d' \
      -e '/^[[:space:]]*https_proxy=/d' \
      -e '/^[[:space:]]*no_proxy=/d' \
      "$ENV_FILE" >"$tmp"
  else
    sed \
      -e '/^[[:space:]]*HTTP_PROXY=/d' \
      -e '/^[[:space:]]*HTTPS_PROXY=/d' \
      -e '/^[[:space:]]*NO_PROXY=/d' \
      -e '/^[[:space:]]*http_proxy=/d' \
      -e '/^[[:space:]]*https_proxy=/d' \
      -e '/^[[:space:]]*no_proxy=/d' \
      -e '/^[[:space:]]*ALL_PROXY=/d' \
      -e '/^[[:space:]]*all_proxy=/d' \
      "$ENV_FILE" >"$tmp"
  fi
else
  : >"$tmp"
fi

{
  # Write uppercase and lowercase variants for compatibility.
  [[ -n "$HTTP_V" ]] && echo "HTTP_PROXY=$HTTP_V"
  [[ -n "$HTTPS_V" ]] && echo "HTTPS_PROXY=$HTTPS_V"
  [[ -n "$NO_V" ]] && echo "NO_PROXY=$NO_V"
  [[ -n "$HTTP_V" ]] && echo "http_proxy=$HTTP_V"
  [[ -n "$HTTPS_V" ]] && echo "https_proxy=$HTTPS_V"
  [[ -n "$NO_V" ]] && echo "no_proxy=$NO_V"
  if [[ "$KEEP_ALL_PROXY" == "1" && -n "$ALL_V" ]]; then
    echo "ALL_PROXY=$ALL_V"
    echo "all_proxy=$ALL_V"
  fi
} >>"$tmp"

install -m "$MODE" "$tmp" "$ENV_FILE"
