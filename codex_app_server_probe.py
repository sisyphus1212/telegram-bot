import argparse
import asyncio
import os
from pathlib import Path

from codex_app_server_client import CodexAppServerConfig, CodexAppServerProcess


def _log(msg: str) -> None:
    print(msg, flush=True)


async def main_async() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    # Default to this directory so `telegram/` can be extracted as a standalone project.
    ap.add_argument("--cwd", default=os.environ.get("CODEX_CWD", str(Path(__file__).resolve().parent)))
    ap.add_argument("--prompt", default="ping")
    ap.add_argument("--sandbox", default="workspace-write", choices=["read-only", "workspace-write", "danger-full-access"])
    ap.add_argument("--approval", default="on-request", choices=["untrusted", "on-failure", "on-request", "never"])
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    proc = CodexAppServerProcess(
        CodexAppServerConfig(codex_bin=args.codex_bin, cwd=args.cwd),
        on_log=_log,
    )
    await proc.start()
    await proc.initialize(client_name="telegram_probe", version="0.0")
    thread_id = await proc.thread_start(cwd=args.cwd, sandbox=args.sandbox, approval_policy=args.approval)
    _log(f"thread_id={thread_id}")

    turn_id = await proc.turn_start_text(thread_id=thread_id, text=args.prompt)
    _log(f"turn_id={turn_id}")

    reply = await proc.run_turn_and_collect_agent_message(thread_id=thread_id, turn_id=turn_id, timeout_s=args.timeout)
    _log("reply:")
    _log(reply or "(empty)")

    await proc.stop()
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
