#!/usr/bin/env bash
set -euo pipefail
cd /root/work/telegram-bot
LOCK=/tmp/void_node_runner.lock
exec 9>"$LOCK"
flock -n 9 || exit 0
while true; do
  .venv/bin/python codex_node.py --config node_config.void.json >>/tmp/void_node.log 2>&1 || true
  sleep 1
done
