import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class CodexAppServerConfig:
    codex_bin: str = "codex"
    cwd: str | None = None
    env: dict[str, str] | None = None


class CodexAppServerError(RuntimeError):
    pass


def _json_dumps_line(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class CodexAppServerProcess:
    """
    Minimal stdio JSON-RPC 2.0 client for `codex app-server`.

    Wire format is JSONL where each line is a single JSON-RPC message. The on-wire
    objects omit the `jsonrpc:"2.0"` field (Codex-specific convention).
    """

    def __init__(
        self,
        config: CodexAppServerConfig,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._on_log = on_log
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stdout: asyncio.StreamReader | None = None
        self._stdin: asyncio.StreamWriter | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[JsonDict]] = {}
        self._notifications: asyncio.Queue[JsonDict] = asyncio.Queue()
        self._initialized = False

    def _log(self, msg: str) -> None:
        if self._on_log:
            self._on_log(msg)

    async def start(self) -> None:
        if self.is_running():
            return
        if self._proc and self._proc.returncode is not None:
            # Clean up any dead process state before restarting.
            try:
                await self.stop()
            except Exception:
                pass

        cwd = self._config.cwd or str(Path.cwd())
        env = os.environ.copy()
        if self._config.env:
            env.update(self._config.env)

        self._proc = await asyncio.create_subprocess_exec(
            self._config.codex_bin,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        assert self._proc.stdout is not None
        assert self._proc.stdin is not None
        self._stdout = self._proc.stdout
        # asyncio already wraps subprocess stdin as a StreamWriter.
        self._stdin = self._proc.stdin

        self._reader_task = asyncio.create_task(self._reader_loop(), name="codex_app_server_reader")
        asyncio.create_task(self._stderr_pump(), name="codex_app_server_stderr")
        self._initialized = False

    async def stop(self) -> None:
        if not self._proc:
            return
        proc = self._proc
        self._proc = None

        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None

        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        self._initialized = False

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def ensure_started_and_initialized(self, client_name: str, version: str) -> None:
        if not self.is_running():
            await self.start()
        if not self._initialized:
            await self.initialize(client_name=client_name, version=version)
            self._initialized = True

    async def _stderr_pump(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                s = line.decode("utf-8", errors="ignore").rstrip()
                if s:
                    self._log(f"[app-server stderr] {s}")
        except asyncio.CancelledError:
            return

    async def _reader_loop(self) -> None:
        assert self._stdout is not None
        try:
            while True:
                line = await self._stdout.readline()
                if not line:
                    raise CodexAppServerError("app-server stdout closed")
                s = line.decode("utf-8", errors="ignore").strip()
                if not s:
                    continue
                try:
                    msg = json.loads(s)
                except Exception:
                    self._log(f"[app-server] non-json line: {s[:200]}")
                    continue

                # Client responses: {"id": <int>, "result": {...}} or {"id": <int>, "error": {...}}
                if (
                    isinstance(msg, dict)
                    and "id" in msg
                    and ("result" in msg or "error" in msg)
                    and "method" not in msg
                ):
                    req_id = msg.get("id")
                    if isinstance(req_id, int) and req_id in self._pending:
                        fut = self._pending.pop(req_id)
                        if "error" in msg and msg["error"] is not None:
                            fut.set_exception(CodexAppServerError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result") or {})
                    continue

                # Server requests: {"id": <int>, "method": "...", "params": {...}}
                if isinstance(msg, dict) and "id" in msg and "method" in msg:
                    asyncio.create_task(self._handle_server_request(msg))
                    continue

                # Notifications: {"method": "...", "params": {...}}
                if isinstance(msg, dict) and "method" in msg and "id" not in msg:
                    await self._notifications.put(msg)
                    continue

                self._log(f"[app-server] unhandled msg: {str(msg)[:200]}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            self._initialized = False
            self._log(f"[app-server] reader loop failed: {e}")
            # Fail any pending requests so callers can retry by restarting.
            for req_id, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(CodexAppServerError("app-server reader stopped"))
            self._pending.clear()
            return

    async def _handle_server_request(self, req: JsonDict) -> None:
        req_id = req.get("id")
        method = req.get("method")
        if not isinstance(req_id, int) or not isinstance(method, str):
            return

        # For stability and safety in automation, default to declining approvals.
        if method == "item/commandExecution/requestApproval":
            await self._send_raw({"id": req_id, "result": {"decision": "decline"}})
            return
        if method == "item/fileChange/requestApproval":
            await self._send_raw({"id": req_id, "result": {"decision": "decline"}})
            return

        # Unknown server-initiated request: reply with a generic JSON-RPC error.
        await self._send_raw(
            {
                "id": req_id,
                "error": {"code": -32601, "message": f"Unsupported server request: {method}"},
            }
        )

    async def _send_raw(self, msg: JsonDict) -> None:
        if not self._stdin:
            raise CodexAppServerError("app-server stdin unavailable")
        self._stdin.write(_json_dumps_line(msg))
        await self._stdin.drain()

    async def request(self, method: str, params: JsonDict | None) -> JsonDict:
        if not self._proc:
            raise CodexAppServerError("app-server not started")
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        msg: JsonDict = {"id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        await self._send_raw(msg)
        return await fut

    async def notify(self, method: str, params: JsonDict | None = None) -> None:
        msg: JsonDict = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._send_raw(msg)

    async def initialize(self, client_name: str = "telegram_bot", version: str = "0.0") -> JsonDict:
        result = await self.request(
            "initialize",
            {
                "clientInfo": {"name": client_name, "title": "Telegram Bot", "version": version},
                "capabilities": {"experimentalApi": True},
            },
        )
        # Complete handshake.
        await self.notify("initialized")
        return result

    async def thread_start(
        self,
        cwd: str | None,
        sandbox: str = "workspace-write",
        approval_policy: str = "on-request",
        personality: str = "pragmatic",
        base_instructions: str | None = None,
    ) -> str:
        params: JsonDict = {
            "cwd": cwd,
            "sandbox": sandbox,
            "approvalPolicy": approval_policy,
            "personality": personality,
        }
        if base_instructions is not None:
            params["baseInstructions"] = base_instructions
        result = await self.request("thread/start", params)
        thread = result.get("thread") or {}
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexAppServerError(f"thread/start missing id: {result}")
        return thread_id

    async def turn_start_text(
        self,
        thread_id: str,
        text: str,
        sandbox_policy: JsonDict | None = None,
        approval_policy: str | None = None,
    ) -> str:
        params: JsonDict = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
        if sandbox_policy is not None:
            params["sandboxPolicy"] = sandbox_policy
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        result = await self.request("turn/start", params)
        turn = result.get("turn") or {}
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexAppServerError(f"turn/start missing id: {result}")
        return turn_id

    async def run_turn_and_collect_agent_message(
        self,
        thread_id: str,
        turn_id: str,
        timeout_s: float = 120.0,
    ) -> str:
        agent_texts: list[str] = []

        async def _wait():
            while True:
                msg = await self._notifications.get()
                method = msg.get("method")
                params = msg.get("params") or {}

                if method == "error":
                    raise CodexAppServerError(str(params))

                if method == "item/completed":
                    if params.get("threadId") != thread_id or params.get("turnId") != turn_id:
                        continue
                    item = params.get("item") or {}
                    if isinstance(item, dict) and item.get("type") == "agentMessage":
                        text = item.get("text")
                        if isinstance(text, str) and text:
                            agent_texts.append(text)

                if method == "turn/completed":
                    if params.get("threadId") != thread_id:
                        continue
                    turn = params.get("turn") or {}
                    if isinstance(turn, dict) and turn.get("id") == turn_id:
                        return

        await asyncio.wait_for(_wait(), timeout=timeout_s)
        return "\n".join(agent_texts).strip()
