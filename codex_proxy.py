import argparse
import asyncio
import json
import logging
import os
import random
import time
import uuid
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

def _kv(**items: object) -> str:
    parts: list[str] = []
    for k, v in items.items():
        if v is None:
            continue
        s = str(v).replace("\n", "\\n")
        parts.append(f"{k}={s}")
    return " ".join(parts)

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
    def __init__(
        self,
        proxy_id: str,
        token: str,
        manager_ws: str,
        codex_bin: str,
        cwd: str,
        *,
        sandbox: str,
        approval_policy: str,
    ) -> None:
        self.proxy_id = proxy_id
        self.token = token
        self.manager_ws = manager_ws
        self.cwd = cwd
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.app = CodexAppServerStdioProcess(CodexLocalAppServerConfig(codex_bin=codex_bin, cwd=cwd))
        self._thread_ids: dict[str, str] = {}  # thread_key -> thread_id
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._busy = asyncio.Lock()  # single execution loop (proxy feeds Codex sequentially)
        self.max_pending = int(os.environ.get("PROXY_MAX_PENDING", "10") or "10")

    async def _ensure_thread(self, thread_key: str, reset: bool) -> str:
        if reset:
            self._thread_ids.pop(thread_key, None)
        tid = self._thread_ids.get(thread_key)
        if tid:
            return tid
        tid = await self.app.thread_start(
            cwd=self.cwd,
            sandbox=self.sandbox,
            approval_policy=self.approval_policy,
            personality="pragmatic",
            base_instructions=None,
        )
        self._thread_ids[thread_key] = tid
        return tid

    async def _run_task(self, msg: JsonDict) -> JsonDict:
        trace_id = str(msg.get("trace_id") or "") or uuid.uuid4().hex
        task_id = str(msg.get("task_id") or "")
        thread_key = str(msg.get("thread_key") or "")
        prompt = str(msg.get("prompt") or "")
        reset_thread = bool(msg.get("reset_thread", False))
        if not task_id or not thread_key:
            return {
                "type": "task_result",
                "trace_id": trace_id,
                "task_id": task_id or "?",
                "ok": False,
                "error": "missing task_id/thread_key",
            }

        async with self._busy:
            t0 = time.time()
            logger.info(
                _kv(
                    op="codex.turn.start",
                    proxy_id=self.proxy_id,
                    trace_id=trace_id,
                    task_id=task_id,
                    prompt_len=len(prompt),
                    reset_thread=reset_thread,
                )
            )
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
                latency_ms = int((time.time() - t0) * 1000.0)
                logger.info(
                    _kv(
                        op="codex.turn.done",
                        proxy_id=self.proxy_id,
                        trace_id=trace_id,
                        task_id=task_id,
                        ok=True,
                        latency_ms=latency_ms,
                        text_len=len((text or "").strip()),
                    )
                )
                return {"type": "task_result", "trace_id": trace_id, "task_id": task_id, "ok": True, "text": (text or "").strip()}
            except CodexAppServerError as e:
                latency_ms = int((time.time() - t0) * 1000.0)
                logger.info(
                    _kv(
                        op="codex.turn.done",
                        proxy_id=self.proxy_id,
                        trace_id=trace_id,
                        task_id=task_id,
                        ok=False,
                        latency_ms=latency_ms,
                        error=str(e),
                    )
                )
                return {"type": "task_result", "trace_id": trace_id, "task_id": task_id, "ok": False, "error": str(e)}
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                latency_ms = int((time.time() - t0) * 1000.0)
                logger.info(
                    _kv(
                        op="codex.turn.done",
                        proxy_id=self.proxy_id,
                        trace_id=trace_id,
                        task_id=task_id,
                        ok=False,
                        latency_ms=latency_ms,
                        error=err,
                    )
                )
                return {"type": "task_result", "trace_id": trace_id, "task_id": task_id, "ok": False, "error": err}

    async def run_forever(self) -> None:
        try:
            # Keep local app-server warm even if manager is temporarily down.
            await self.app.start()
            await self.app.ensure_started_and_initialized(client_name=f"codex_proxy:{self.proxy_id}", version="0.0")

            backoff_s = 0.5
            while True:
                try:
                    async with websockets.connect(self.manager_ws, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024) as ws:
                        send_lock = asyncio.Lock()
                        exec_queue: asyncio.Queue[JsonDict] = asyncio.Queue()
                        pending_count = 0

                        await ws.send(_json_dumps({"type": "register", "proxy_id": self.proxy_id, "token": self.token}))
                        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        rep = json.loads(raw)
                        if not isinstance(rep, dict) or rep.get("type") != "register_ok":
                            err = rep.get("error") if isinstance(rep, dict) else "bad reply"
                            raise RuntimeError(f"register failed: {err}")

                        logger.info(_kv(op="register.ok", proxy_id=self.proxy_id, manager_ws=self.manager_ws, sandbox=self.sandbox, approval_policy=self.approval_policy))
                        backoff_s = 0.5
                        stop_hb = asyncio.Event()
                        stop_exec = asyncio.Event()

                        async def _hb_loop() -> None:
                            while not stop_hb.is_set():
                                try:
                                    async with send_lock:
                                        await ws.send(_json_dumps({"type": "heartbeat", "proxy_id": self.proxy_id}))
                                except Exception:
                                    return
                                await asyncio.sleep(10.0)

                        async def _exec_loop() -> None:
                            nonlocal pending_count
                            while not stop_exec.is_set():
                                try:
                                    msg = await exec_queue.get()
                                except Exception:
                                    continue
                                try:
                                    out = await self._run_task(msg)
                                except Exception as e:
                                    trace_id = str(msg.get("trace_id") or "") or uuid.uuid4().hex
                                    task_id = str(msg.get("task_id") or "?")
                                    out = {
                                        "type": "task_result",
                                        "trace_id": trace_id,
                                        "task_id": task_id,
                                        "ok": False,
                                        "error": f"{type(e).__name__}: {e}",
                                    }
                                try:
                                    logger.info(
                                        _kv(
                                            op="ws.send",
                                            proxy_id=self.proxy_id,
                                            type="task_result",
                                            trace_id=out.get("trace_id"),
                                            task_id=out.get("task_id"),
                                            ok=bool(out.get("ok")),
                                            error=out.get("error") if not out.get("ok") else None,
                                        )
                                    )
                                    async with send_lock:
                                        await ws.send(_json_dumps(out))
                                except Exception:
                                    # Connection likely gone; manager will timeout.
                                    pass
                                finally:
                                    pending_count = max(0, pending_count - 1)

                        hb_task = asyncio.create_task(_hb_loop(), name="heartbeat")
                        exec_task = asyncio.create_task(_exec_loop(), name="exec_loop")
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
                                task_id = msg.get("task_id")
                                trace_id = str(msg.get("trace_id") or "") or uuid.uuid4().hex
                                msg["trace_id"] = trace_id
                                logger.info(_kv(op="ws.recv", proxy_id=self.proxy_id, type="task_assign", trace_id=trace_id, task_id=task_id))
                                if pending_count >= self.max_pending:
                                    # Refuse immediately so manager can show error quickly.
                                    out = {
                                        "type": "task_result",
                                        "trace_id": trace_id,
                                        "task_id": str(task_id or "?"),
                                        "ok": False,
                                        "error": f"proxy queue full (max={self.max_pending})",
                                    }
                                    try:
                                        async with send_lock:
                                            await ws.send(_json_dumps(out))
                                    except Exception:
                                        pass
                                    continue

                                pending_count += 1
                                logger.info(_kv(op="exec.enqueue", proxy_id=self.proxy_id, trace_id=trace_id, task_id=task_id, pending=pending_count, max_pending=self.max_pending))
                                try:
                                    async with send_lock:
                                        await ws.send(_json_dumps({"type": "task_ack", "trace_id": trace_id, "task_id": task_id}))
                                except Exception:
                                    pending_count = max(0, pending_count - 1)
                                    continue

                                # Queue for sequential Codex feeding (FIFO).
                                try:
                                    exec_queue.put_nowait(msg)
                                except Exception:
                                    pending_count = max(0, pending_count - 1)
                                    out = {
                                        "type": "task_result",
                                        "trace_id": trace_id,
                                        "task_id": str(task_id or "?"),
                                        "ok": False,
                                        "error": "proxy enqueue failed",
                                    }
                                    try:
                                        async with send_lock:
                                            await ws.send(_json_dumps(out))
                                    except Exception:
                                        pass
                        finally:
                            stop_hb.set()
                            stop_exec.set()
                            hb_task.cancel()
                            exec_task.cancel()
                            await asyncio.gather(hb_task, return_exceptions=True)
                            await asyncio.gather(exec_task, return_exceptions=True)
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
    ap.add_argument("--sandbox", default="", help="override codex sandbox (e.g. workspace-write / danger-full-access)")
    ap.add_argument("--approval-policy", default="", help="override approval policy (e.g. on-request / never)")
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
    max_pending = int(os.environ.get("PROXY_MAX_PENDING") or str(cfg.get("max_pending") or 10))
    sandbox = args.sandbox or os.environ.get("CODEX_SANDBOX") or _cfg_get_str(cfg, "sandbox") or "workspace-write"
    approval_policy = args.approval_policy or os.environ.get("CODEX_APPROVAL_POLICY") or _cfg_get_str(cfg, "approval_policy") or "on-request"

    if not proxy_id:
        raise SystemExit("missing PROXY_ID / --proxy-id")
    # Dev mode: allow empty PROXY_TOKEN if manager doesn't enforce an allowlist.

    agent = CodexProxyAgent(
        proxy_id=proxy_id,
        token=token,
        manager_ws=manager_ws,
        codex_bin=codex_bin,
        cwd=cwd,
        sandbox=sandbox,
        approval_policy=approval_policy,
    )
    agent.max_pending = max(1, max_pending)
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
