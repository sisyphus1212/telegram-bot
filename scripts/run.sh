#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing venv. Run: scripts/install.sh" >&2
  exit 1
fi

# Keep proxy env clean for python-telegram-bot/httpx:
# - Do not force any proxy here (deploy should inject HTTP_PROXY/HTTPS_PROXY into systemd env).
# - Only remove ALL_PROXY/all_proxy to avoid SOCKS proxy auto-detection.
unset ALL_PROXY all_proxy

exec .venv/bin/python codex_manager.py
