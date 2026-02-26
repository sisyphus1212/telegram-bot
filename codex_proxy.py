import argparse
import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

import websockets

from codex_stdio_client import CodexAppServerError, CodexAppServerStdioProcess, CodexLocalAppServerConfig


JsonDict = dict[str, Any]
BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("codex_proxy")

def _load_json(path: Path) -> JsonDict:
    try:
        raw = path.read_text("utf-8")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        raise SystemExit(f"failed to load config {str(path)!r}: {e}")


def _cfg_get_str(cfg: JsonDict, key: str) -> str:
    v = cfg.get(key)
    return v.strip() if isinstance(v, str) else ""


def _maybe_set_env(name: str, value: str) -> None:
    if not value:
        return
    if os.environ.get(name):
        return
    os.environ[name] = value


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class CodexProxyAgent:
    def __init__(self, proxy_id: str, token: str, manager_ws: str, codex_bin: str, cwd: str) -> None:
        self.proxy_id = proxy_id
        self.token = token
        self.manager_ws = manager_ws
        self.cwd = cwd
        self.app = CodexAppServerStdioProcess(CodexLocalAppServerConfig(codex_bin=codex_bin, cwd=cwd))
        self._thread_ids: dict[str, str] = {}  # thread_key -> thread_id
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._busy = asyncio.Lock()  # minimal: single-task execution

    async def _ensure_thread(self, thread_key: str, reset: bool) -> str:
        if reset:
            self._thread_ids.pop(thread_key, None)
        tid = self._thread_ids.get(thread_key)
        if tid:
            return tid
        tid = await self.app.thread_start(
            cwd=self.cwd,
            sandbox="workspace-write",
            approval_policy="on-request",
            personality="pragmatic",
            base_instructions=None,
        )
        self._thread_ids[thread_key] = tid
        return tid

    async def _run_task(self, msg: JsonDict) -> JsonDict:
        task_id = str(msg.get("task_id") or "")
        thread_key = str(msg.get("thread_key") or "")
        prompt = str(msg.get("prompt") or "")
        reset_thread = bool(msg.get("reset_thread", False))
        if not task_id or not thread_key:
            return {"type": "task_result", "task_id": task_id or "?", "ok": False, "error": "missing task_id/thread_key"}

        if self._busy.locked():
            return {"type": "task_result", "task_id": task_id, "ok": False, "error": "busy"}

        async with self._busy:
            try:
                await self.app.ensure_started_and_initialized(client_name=f"codex_proxy:{self.proxy_id}", version="0.0")
                thread_id = await self._ensure_thread(thread_key=thread_key, reset=reset_thread)
                lock = self._thread_locks.get(thread_id)
                if lock is None:
                    lock = asyncio.Lock()
                    self._thread_locks[thread_id] = lock
                async with lock:
                    turn_id = await self.app.turn_start_text(thread_id=thread_id, text=prompt)
                    text = await self.app.run_turn_and_collect_agent_message(thread_id=thread_id, turn_id=turn_id, timeout_s=120.0)
                return {"type": "task_result", "task_id": task_id, "ok": True, "text": (text or "").strip()}
            except CodexAppServerError as e:
                return {"type": "task_result", "task_id": task_id, "ok": False, "error": str(e)}
            except Exception as e:
                return {"type": "task_result", "task_id": task_id, "ok": False, "error": f"{type(e).__name__}: {e}"}

    async def run_forever(self) -> None:
        try:
            # Keep local app-server warm even if manager is temporarily down.
            await self.app.start()
            await self.app.ensure_started_and_initialized(client_name=f"codex_proxy:{self.proxy_id}", version="0.0")

            backoff_s = 0.5
            while True:
                try:
                    async with websockets.connect(self.manager_ws, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024) as ws:
                        await ws.send(_json_dumps({"type": "register", "proxy_id": self.proxy_id, "token": self.token}))
                        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        rep = json.loads(raw)
                        if not isinstance(rep, dict) or rep.get("type") != "register_ok":
                            err = rep.get("error") if isinstance(rep, dict) else "bad reply"
                            raise RuntimeError(f"register failed: {err}")

                        logger.info(f"registered to manager as {self.proxy_id} ({self.manager_ws})")
                        backoff_s = 0.5
                        stop_hb = asyncio.Event()

                        async def _hb_loop() -> None:
                            while not stop_hb.is_set():
                                try:
                                    await ws.send(_json_dumps({"type": "heartbeat", "proxy_id": self.proxy_id}))
                                except Exception:
                                    return
                                await asyncio.sleep(10.0)

                        hb_task = asyncio.create_task(_hb_loop(), name="heartbeat")
                        try:
                            async for raw in ws:
                                try:
                                    msg = json.loads(raw)
                                except Exception:
                                    continue
                                if not isinstance(msg, dict):
                                    continue
                                if msg.get("type") != "task_assign":
                                    continue
                                out = await self._run_task(msg)
                                await ws.send(_json_dumps(out))
                        finally:
                            stop_hb.set()
                            hb_task.cancel()
                            await asyncio.gather(hb_task, return_exceptions=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Manager down / network blip. Retry with backoff.
                    logger.warning(f"manager connection failed; retrying in {backoff_s:.1f}s")
                    await asyncio.sleep(backoff_s + random.random() * 0.2)
                    backoff_s = min(10.0, backoff_s * 1.7)
        finally:
            try:
                await self.app.stop()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.environ.get("CODEX_PROXY_CONFIG") or "", help="path to proxy_config.json")
    ap.add_argument("--manager-ws", default="", help="override manager WS (ws://host:port)")
    ap.add_argument("--proxy-id", default="", help="override proxy id")
    ap.add_argument("--token", default="", help="override proxy token")
    ap.add_argument("--codex-bin", default="", help="override codex bin")
    ap.add_argument("--cwd", default="", help="override codex cwd")
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else (BASE_DIR / "proxy_config.json" if (BASE_DIR / "proxy_config.json").exists() else None)
    cfg = _load_json(cfg_path) if cfg_path else {}

    # Optional network proxy config for Telegram/codex connectivity on some hosts.
    _maybe_set_env("HTTP_PROXY", _cfg_get_str(cfg, "http_proxy"))
    _maybe_set_env("HTTPS_PROXY", _cfg_get_str(cfg, "https_proxy"))
    _maybe_set_env("NO_PROXY", _cfg_get_str(cfg, "no_proxy"))

    manager_ws = args.manager_ws or os.environ.get("CODEX_MANAGER_WS") or _cfg_get_str(cfg, "manager_ws") or "ws://127.0.0.1:8765"
    proxy_id = args.proxy_id or os.environ.get("PROXY_ID") or _cfg_get_str(cfg, "proxy_id")
    token = args.token or os.environ.get("PROXY_TOKEN") or _cfg_get_str(cfg, "proxy_token")
    codex_bin = args.codex_bin or os.environ.get("CODEX_BIN") or _cfg_get_str(cfg, "codex_bin") or "codex"
    cwd = args.cwd or os.environ.get("CODEX_CWD") or _cfg_get_str(cfg, "codex_cwd") or str(BASE_DIR)

    if not proxy_id:
        raise SystemExit("missing PROXY_ID / --proxy-id")
    # Dev mode: allow empty PROXY_TOKEN if manager doesn't enforce an allowlist.

    agent = CodexProxyAgent(
        proxy_id=proxy_id,
        token=token,
        manager_ws=manager_ws,
        codex_bin=codex_bin,
        cwd=cwd,
    )
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
