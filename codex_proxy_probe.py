import argparse
import asyncio
import json
import os
from pathlib import Path

from codex_stdio_client import CodexAppServerStdioProcess, CodexLocalAppServerConfig


async def _run(prompt: str, cwd: str, codex_bin: str) -> dict:
    app = CodexAppServerStdioProcess(CodexLocalAppServerConfig(codex_bin=codex_bin, cwd=cwd))
    await app.start()
    await app.ensure_started_and_initialized(client_name="codex_proxy_probe", version="0.0")
    tid = await app.thread_start(cwd=cwd, sandbox="workspace-write", approval_policy="on-request", personality="pragmatic")
    turn_id = await app.turn_start_text(thread_id=tid, text=prompt)
    text = await app.run_turn_and_collect_agent_message(thread_id=tid, turn_id=turn_id, timeout_s=60.0)
    await app.stop()
    return {"ok": True, "threadId": tid, "turnId": turn_id, "text": (text or "").strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="ping")
    ap.add_argument("--cwd", default=os.environ.get("CODEX_CWD") or str(Path(__file__).resolve().parent))
    ap.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN") or "codex")
    args = ap.parse_args()

    out = asyncio.run(_run(args.prompt, cwd=args.cwd, codex_bin=args.codex_bin))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

