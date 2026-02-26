#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x ".venv/bin/python" ]]; then
  echo "missing venv: run ./scripts/install.sh first" >&2
  exit 2
fi

. .venv/bin/activate

python - <<'PY'
import asyncio, json, time
from codex_stdio_client import CodexAppServerStdioProcess, CodexLocalAppServerConfig

async def main():
    app = CodexAppServerStdioProcess(CodexLocalAppServerConfig(codex_bin="codex", cwd="/root/telegram-bot"))
    await app.start()
    await app.ensure_started_and_initialized(client_name="verify_appserver_methods", version="0")
    try:
        t0=time.time()
        r = await app.request("thread/start", {"cwd": "/root/telegram-bot", "sandbox": "workspace-write", "approvalPolicy": "on-request", "personality": "pragmatic"})
        tid = (r.get("thread") or {}).get("id")
        assert isinstance(tid, str) and tid, f"bad thread id: {r!r}"

        r2 = await app.request("thread/read", {"threadId": tid, "includeTurns": False})
        assert isinstance(r2, dict), r2

        r3 = await app.request("thread/list", {"limit": 2})
        assert isinstance(r3.get("data"), list), r3

        r4 = await app.request("model/list", {"limit": 3})
        assert isinstance(r4.get("data"), list), r4

        r5 = await app.request("skills/list", {"cwds": ["/root/telegram-bot"]})
        assert isinstance(r5, dict), r5

        dt=int((time.time()-t0)*1000)
        print(json.dumps({"ok": True, "latency_ms": dt, "thread_id": tid}, ensure_ascii=False))
    finally:
        await app.stop()

asyncio.run(main())
PY

