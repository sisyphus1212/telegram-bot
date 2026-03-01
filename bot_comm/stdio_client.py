import asyncio
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


JsonDict = dict[str, Any]


class CodexAppServerError(RuntimeError):
    pass


def _json_dumps_line(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class CodexLocalAppServerConfig:
    codex_bin: str = "codex"
    cwd: str | None = None
    env: dict[str, str] | None = None


class CodexAppServerStdioProcess:
    """
    Minimal stdio JSON-RPC 2.0 client for `codex app-server` (JSONL over stdio).
    """

    def __init__(
        self,
        config: CodexLocalAppServerConfig,
        on_log: Callable[[str], None] | None = None,
        *,
        on_approval_request: Callable[[JsonDict], Awaitable[str]] | None = None,
        on_notification: Callable[[JsonDict], Awaitable[None] | None] | None = None,
    ) -> None:
        self._config = config
        self._on_log = on_log
        self._on_approval_request = on_approval_request
        self._on_notification = on_notification
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

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self.is_running():
            return

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
        self._stdin = self._proc.stdin
        self._reader_task = asyncio.create_task(self._reader_loop(), name="codex_app_server_stdio_reader")
        asyncio.create_task(self._stderr_pump(), name="codex_app_server_stdio_stderr")
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
            # asyncio StreamReader.readline() has an internal line length limit (64KiB default).
            # app-server can emit long stderr lines; read in chunks and split manually.
            buf = bytearray()
            while True:
                chunk = await self._proc.stderr.read(65536)
                if not chunk:
                    if buf:
                        s = bytes(buf).decode("utf-8", errors="ignore").rstrip()
                        if s:
                            self._log(f"[app-server stderr] {s}")
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    s = line.decode("utf-8", errors="ignore").rstrip()
                    if s:
                        self._log(f"[app-server stderr] {s}")
        except asyncio.CancelledError:
            return

    async def _send_raw(self, msg: JsonDict) -> None:
        if not self._stdin:
            raise CodexAppServerError("app-server stdin unavailable")
        self._stdin.write(_json_dumps_line(msg))
        await self._stdin.drain()

    async def request(self, method: str, params: JsonDict | None) -> JsonDict:
        if not self.is_running():
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

    async def initialize(self, client_name: str, version: str) -> JsonDict:
        result = await self.request(
            "initialize",
            {
                "clientInfo": {"name": client_name, "title": "Codex Node", "version": version},
                "capabilities": {"experimentalApi": True},
            },
        )
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
        # NOTE: Different codex versions have slightly different enum spellings.
        # - thread/start sandbox: legacy kebab-case in some versions (workspace-write/read-only/danger-full-access)
        # - thread/start approvalPolicy: legacy kebab-case or other variants (on-request/untrusted/never/...)
        sandbox = {
            "workspaceWrite": "workspace-write",
            "readOnly": "read-only",
            "dangerFullAccess": "danger-full-access",
        }.get(sandbox, sandbox)
        approval_policy = {
            "onRequest": "on-request",
            "unlessTrusted": "untrusted",
        }.get(approval_policy, approval_policy)
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

    async def thread_resume(self, thread_id: str) -> JsonDict:
        return await self.request("thread/resume", {"threadId": thread_id})

    async def thread_list(self, params: JsonDict | None = None) -> JsonDict:
        return await self.request("thread/list", params or {})

    async def thread_read(self, thread_id: str, include_turns: bool = False) -> JsonDict:
        return await self.request("thread/read", {"threadId": thread_id, "includeTurns": bool(include_turns)})

    async def thread_archive(self, thread_id: str) -> JsonDict:
        return await self.request("thread/archive", {"threadId": thread_id})

    async def thread_unarchive(self, thread_id: str) -> JsonDict:
        return await self.request("thread/unarchive", {"threadId": thread_id})

    async def thread_loaded_list(self) -> JsonDict:
        return await self.request("thread/loaded/list", {})

    async def model_list(self, params: JsonDict | None = None) -> JsonDict:
        return await self.request("model/list", params or {})

    async def skills_list(self, params: JsonDict | None = None) -> JsonDict:
        return await self.request("skills/list", params or {})

    async def config_read(self, params: JsonDict | None = None) -> JsonDict:
        return await self.request("config/read", params or {})

    async def config_value_write(self, params: JsonDict) -> JsonDict:
        return await self.request("config/value/write", params)

    async def collaborationmode_list(self) -> JsonDict:
        return await self.request("collaborationMode/list", {})

    async def turn_start_text(self, thread_id: str, text: str, *, model: str | None = None, effort: str | None = None) -> str:
        params: JsonDict = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
        # Per app-server spec: turn/start supports per-turn overrides (e.g. model/effort)
        # and will update the thread defaults for subsequent turns.
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        result = await self.request("turn/start", params)
        turn = result.get("turn") or {}
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexAppServerError(f"turn/start missing id: {result}")
        return turn_id

    async def run_turn_and_collect_agent_message(self, thread_id: str, turn_id: str, timeout_s: float = 120.0) -> str:
        agent_texts: list[str] = []

        async def _wait():
            while True:
                msg = await self._notifications.get()
                method = msg.get("method")
                params = msg.get("params") or {}

                if method == "error":
                    # Some codex versions emit transient errors with `willRetry=true`, e.g.
                    # {"message":"Reconnecting... 1/5", "willRetry":true, ...}
                    # Treat these as progress notifications; only fail on non-retriable errors.
                    if isinstance(params, dict) and bool(params.get("willRetry")):
                        # Keep logs short; upstream network reconnects are common and noisy.
                        # Note: avoid logging large nested error payloads here.
                        msg = ""
                        try:
                            err = params.get("error") if isinstance(params, dict) else None
                            if isinstance(err, dict):
                                msg = str(err.get("message") or "")
                            if not msg:
                                msg = str(params.get("message") or "")
                        except Exception:
                            msg = ""
                        self._log(f"[app-server] transient error (willRetry=true): {msg[:200] or 'reconnecting'}")
                        continue
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

    async def _handle_server_request(self, req: JsonDict) -> None:
        req_id = req.get("id")
        method = req.get("method")
        if not isinstance(req_id, int) or not isinstance(method, str):
            return

        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            # Let the outer application decide; if no handler, default to decline for safety.
            decision = "decline"
            if self._on_approval_request is not None:
                try:
                    decision = (await self._on_approval_request(req)) or "decline"
                except Exception as e:
                    self._log(f"[app-server] approval handler failed: {type(e).__name__}: {e}")
                    decision = "decline"
            await self._send_raw({"id": req_id, "result": {"decision": decision}})
            return
        await self._send_raw({"id": req_id, "error": {"code": -32601, "message": f"Unsupported request: {method}"}})

    async def _dispatch_notification(self, msg: JsonDict) -> None:
        if self._on_notification is None:
            return
        try:
            ret = self._on_notification(msg)
            if asyncio.iscoroutine(ret):
                await ret
        except Exception as e:
            self._log(f"[app-server] notification handler failed: {type(e).__name__}: {e}")

    async def _reader_loop(self) -> None:
        assert self._stdout is not None
        try:
            # asyncio StreamReader.readline() has an internal line length limit (64KiB default).
            # app-server can emit JSONL lines larger than that (e.g. long agentMessage), which
            # would crash the reader loop. Read in chunks and split on '\n' ourselves.
            buf = bytearray()
            max_line_bytes = 32 * 1024 * 1024  # guardrail against unbounded growth if newline never arrives
            while True:
                chunk = await self._stdout.read(65536)
                if not chunk:
                    # EOF: process any trailing partial line, then treat as closed.
                    if buf:
                        lines = [bytes(buf)]
                        buf.clear()
                    else:
                        lines = []
                    if not lines:
                        raise CodexAppServerError("app-server stdout closed")
                else:
                    buf.extend(chunk)
                    if len(buf) > max_line_bytes and b"\n" not in buf:
                        raise CodexAppServerError(f"app-server stdout line exceeds {max_line_bytes} bytes (no newline)")
                    lines = []
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line = bytes(buf[:nl])
                        del buf[: nl + 1]
                        lines.append(line)

                for line in lines:
                    s = line.decode("utf-8", errors="ignore").strip()
                    if not s:
                        continue
                    try:
                        msg = json.loads(s)
                    except Exception:
                        self._log(f"[app-server] non-json line: {s[:200]}")
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
                        req_id = msg.get("id")
                        if isinstance(req_id, int) and req_id in self._pending:
                            fut = self._pending.pop(req_id)
                            if "error" in msg and msg["error"] is not None:
                                fut.set_exception(CodexAppServerError(str(msg["error"])))
                            else:
                                fut.set_result(msg.get("result") or {})
                        continue
                    if "id" in msg and "method" in msg:
                        asyncio.create_task(self._handle_server_request(msg))
                        continue
                    if "method" in msg and "id" not in msg:
                        asyncio.create_task(self._dispatch_notification(msg))
                        await self._notifications.put(msg)
                        continue
        except asyncio.CancelledError:
            return
        except Exception as e:
            self._initialized = False
            self._log(f"[app-server] reader loop failed: {e}")
            for _req_id, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(CodexAppServerError("app-server reader stopped"))
            self._pending.clear()
            return
