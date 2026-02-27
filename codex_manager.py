import argparse
import asyncio
import json
import logging
import os
import signal
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import websockets
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.request import HTTPXRequest

import log_rotate


JsonDict = dict[str, Any]

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "log"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "manager.log"
SESSIONS_FILE = BASE_DIR / "sessions.json"
CONFIG_FILE = BASE_DIR / "manager_config.json"


def setup_logging() -> logging.Logger:
    log_rotate.rotate_logs(
        LOG_DIR,
        "manager.log",
        max_lines=5000,
        max_backups=3,
        archive_name_prefix="manager_log_archive",
        compression_level=4,
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    # python-telegram-bot uses httpx; httpx INFO logs include full request URLs (bot token in path).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger("codex_manager")


logger = setup_logging()

async def _tg_call(coro, *, timeout_s: float, what: str, retries: int = 3) -> Any:
    """
    Telegram 网络在部分环境下不稳定（尤其是走本地代理时），这里做轻量重试避免“没反应”。

    注意：send_message 不是严格幂等，重试可能导致重复消息；但比起完全无响应更可接受。
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await asyncio.wait_for(coro, timeout=timeout_s)
        except RetryAfter as e:
            # Telegram 限流：按建议等待后重试。
            wait_s = max(0.5, float(getattr(e, "retry_after", 1.0)))
            logger.warning(f"telegram call rate-limited ({what}): RetryAfter {wait_s:.1f}s (attempt {attempt}/{retries})")
            if attempt >= retries:
                raise
            await asyncio.sleep(wait_s)
        except (TimedOut, NetworkError, asyncio.TimeoutError) as e:
            logger.warning(f"telegram call transient failed ({what}): {type(e).__name__}: {e} (attempt {attempt}/{retries})")
            if attempt >= retries:
                raise
            await asyncio.sleep(min(5.0, 0.7 * (1.6 ** (attempt - 1))))
        except Exception as e:
            logger.warning(f"telegram call failed ({what}): {type(e).__name__}: {e}")
            raise


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load config file {CONFIG_FILE}: {e}")
        return {}


def _parse_allowed_user_ids(value: str) -> set[int]:
    if not value:
        return set()
    out: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            logger.warning(f"Invalid TELEGRAM_ALLOWED_USER_IDS entry: {part!r}")
    return out


def _parse_int_list_env(value: str) -> list[int]:
    out: list[int] = []
    for part in (value or "").replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except Exception:
            continue
    return out


def _read_git_short_sha(repo_dir: Path) -> str:
    try:
        git = repo_dir / ".git"
        head_path = git / "HEAD"
        if not head_path.exists():
            return ""
        head = head_path.read_text(encoding="utf-8", errors="replace").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_path = git / ref
            if ref_path.exists():
                sha = ref_path.read_text(encoding="utf-8", errors="replace").strip()
                return sha[:12]
            packed = git / "packed-refs"
            if packed.exists():
                for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("^") or " " not in line:
                        continue
                    sha, name = line.split(" ", 1)
                    if name.strip() == ref:
                        return sha.strip()[:12]
                return ""
        if len(head) >= 12:
            return head[:12]
    except Exception:
        return ""
    return ""


def _split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts


def _prefix_and_split_telegram_text(text: str, prefix: str, limit: int = 3900) -> list[str]:
    """
    Split text into Telegram-safe chunks and add a proxy prefix to each chunk.
    Telegram hard limit is 4096; we keep a buffer via default limit.
    """
    prefix = prefix or ""
    if not prefix:
        return _split_telegram_text(text, limit=limit)
    # Keep at least 200 chars for payload even if prefix is long.
    per_part_limit = max(200, limit - len(prefix))
    parts = _split_telegram_text(text, limit=per_part_limit)
    return [prefix + p for p in parts]


def load_sessions() -> dict[str, dict]:
    """
    sessions.json v2 schema (持久化路由元数据，而不是 Codex thread 内容):

      {
        "version": 2,
        "saved_at": 1700000000,
        "sessions": {
          "tg:<chat>:<user>": {
            "proxy": "proxy1",
            "by_proxy": { "proxy1": { "current_thread_id": "thr_...", "last_used_at": 1700000000 } },
            "defaults": { "cwd": "...", "sandbox": "workspaceWrite", "approvalPolicy": "onRequest", "personality": "pragmatic", "model": "" }
          }
        }
      }
    """
    if not SESSIONS_FILE.exists():
        return {}
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}

        # v2
        if data.get("version") == 2 and isinstance(data.get("sessions"), dict):
            out: dict[str, dict] = {}
            for k, v in data["sessions"].items():
                if not isinstance(k, str) or not k:
                    continue
                if not isinstance(v, dict):
                    continue
                proxy = str(v.get("proxy") or "")
                by_proxy = v.get("by_proxy") if isinstance(v.get("by_proxy"), dict) else {}
                defaults = v.get("defaults") if isinstance(v.get("defaults"), dict) else {}
                result_mode = str(v.get("result_mode") or "send")
                out[k] = {"proxy": proxy, "by_proxy": by_proxy, "defaults": defaults, "result_mode": result_mode}
            return out

        # Legacy migration (v0/v1): { session_key: {proxy, pc_mode, reset_next, ...} }
        upgraded: dict[str, dict] = {}
        for k, v in data.items():
            if not isinstance(k, str) or not k:
                continue
            if isinstance(v, dict):
                upgraded[k] = {"proxy": str(v.get("proxy") or v.get("server") or ""), "by_proxy": {}, "defaults": {}, "result_mode": "send"}
            else:
                upgraded[k] = {"proxy": "", "by_proxy": {}, "defaults": {}, "result_mode": "send"}
        return upgraded
    except Exception as e:
        logger.warning(f"Failed to load sessions: {e}")
        return {}


def save_sessions(sessions: dict[str, dict]) -> None:
    try:
        payload = {"version": 2, "saved_at": int(time.time()), "sessions": sessions}
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save sessions: {e}")


@dataclass
class ProxyConn:
    proxy_id: str
    ws: Any
    last_seen: float
    send_lock: asyncio.Lock


@dataclass
class TaskContext:
    task_id: str
    trace_id: str
    proxy_id: str
    thread_id: str
    chat_id: int
    placeholder_msg_id: int
    created_at: float
    session_key: str = ""
    result_mode: str = "send"
    last_progress_at: float = 0.0
    last_progress_text: str = ""
    pending_progress_text: str = ""
    last_progress_event: str = ""
    progress_lines: list[str] | None = None
    progress_last_message_at: float = 0.0


@dataclass
class ApprovalContext:
    approval_id: str
    proxy_id: str
    task_id: str
    trace_id: str
    rpc_id: int | None
    method: str
    params: JsonDict
    chat_id: int
    created_at: float


@dataclass
class TgAction:
    type: str  # "edit" | "send"
    chat_id: int
    text: str
    message_id: int | None = None
    timeout_s: float = 15.0
    trace_id: str = ""
    proxy_id: str = ""
    task_id: str = ""
    kind: str = ""  # "placeholder" | "result" | "late" | "timeout" | ...


class TelegramOutbox:
    def __init__(self, bot: Any, *, max_active_chats: int = 200, max_queue_per_chat: int = 50, idle_ttl_s: float = 600.0) -> None:
        self.bot = bot
        self.max_active_chats = max_active_chats
        self.max_queue_per_chat = max_queue_per_chat
        self.idle_ttl_s = idle_ttl_s
        self._lock = asyncio.Lock()
        self._queues: dict[int, asyncio.Queue[TgAction]] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._last_active: dict[int, float] = {}

    async def enqueue(self, action: TgAction) -> bool:
        now = time.time()
        async with self._lock:
            q = self._queues.get(action.chat_id)
            if q is None:
                if len(self._queues) >= self.max_active_chats:
                    return False
                q = asyncio.Queue(maxsize=self.max_queue_per_chat)
                self._queues[action.chat_id] = q
                self._last_active[action.chat_id] = now
                self._tasks[action.chat_id] = asyncio.create_task(self._sender_loop(action.chat_id), name=f"tg_sender:{action.chat_id}")
            self._last_active[action.chat_id] = now
            try:
                q.put_nowait(action)
            except asyncio.QueueFull:
                return False
            return True

    async def _sender_loop(self, chat_id: int) -> None:
        try:
            while True:
                # Exit on idle TTL when no pending actions.
                async with self._lock:
                    last = self._last_active.get(chat_id, time.time())
                    q = self._queues.get(chat_id)
                if q is None:
                    return
                if q.empty() and (time.time() - last) > self.idle_ttl_s:
                    async with self._lock:
                        self._queues.pop(chat_id, None)
                        self._last_active.pop(chat_id, None)
                        self._tasks.pop(chat_id, None)
                    return

                try:
                    action = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                try:
                    if action.type == "edit":
                        await _tg_call(
                            self.bot.edit_message_text(chat_id=action.chat_id, message_id=action.message_id, text=action.text),
                            timeout_s=action.timeout_s,
                            what="tg edit",
                        )
                        logger.info(
                            f"op=tg.edit ok=true chat_id={action.chat_id} msg_id={action.message_id} "
                            f"trace_id={action.trace_id} proxy_id={action.proxy_id} task_id={action.task_id} kind={action.kind}"
                        )
                    elif action.type == "send":
                        await _tg_call(self.bot.send_message(chat_id=action.chat_id, text=action.text), timeout_s=action.timeout_s, what="tg send")
                        logger.info(
                            f"op=tg.send ok=true chat_id={action.chat_id} trace_id={action.trace_id} "
                            f"proxy_id={action.proxy_id} task_id={action.task_id} kind={action.kind}"
                        )
                except Exception as e:
                    logger.warning(f"tg outbox send failed chat={chat_id}: {type(e).__name__}: {e}")
                finally:
                    async with self._lock:
                        self._last_active[chat_id] = time.time()
        except asyncio.CancelledError:
            raise


class ProxyRegistry:
    def __init__(self, allowed: dict[str, str]) -> None:
        self._allowed = allowed  # proxy_id -> token
        self._lock = asyncio.Lock()
        self._conns: dict[str, ProxyConn] = {}

    def allowed_proxy_ids(self) -> list[str]:
        return sorted(self._allowed.keys())

    def online_proxy_ids(self) -> list[str]:
        return sorted(self._conns.keys())

    def is_online(self, proxy_id: str) -> bool:
        return proxy_id in self._conns

    async def register(self, proxy_id: str, token: str, ws: Any) -> bool:
        # Dev mode (current phase): accept any proxy registration.
        # If an allowlist is configured, we only use it for warnings (not enforcement).
        if self._allowed:
            expect = self._allowed.get(proxy_id, "")
            if not expect:
                logger.warning(f"proxy_id {proxy_id!r} not in allowlist; accepting (dev mode).")
            elif token != expect:
                logger.warning(f"proxy_id {proxy_id!r} token mismatch; accepting (dev mode).")
        async with self._lock:
            # Replace existing connection if any.
            self._conns[proxy_id] = ProxyConn(proxy_id=proxy_id, ws=ws, last_seen=time.time(), send_lock=asyncio.Lock())
        return True

    async def unregister_if_matches(self, proxy_id: str, ws: Any) -> None:
        async with self._lock:
            cur = self._conns.get(proxy_id)
            if not cur or cur.ws is not ws:
                return
            self._conns.pop(proxy_id, None)

    async def heartbeat(self, proxy_id: str, ws: Any) -> None:
        async with self._lock:
            cur = self._conns.get(proxy_id)
            if cur and cur.ws is ws:
                cur.last_seen = time.time()

    async def send_json(self, proxy_id: str, msg: JsonDict) -> None:
        async with self._lock:
            conn = self._conns.get(proxy_id)
            if not conn:
                raise RuntimeError(f"proxy offline: {proxy_id}")
            ws = conn.ws
            lock = conn.send_lock
        payload = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        trace_id = str(msg.get("trace_id") or "")
        task_id = str(msg.get("task_id") or "")
        async with lock:
            logger.info(
                f"op=ws.send proxy_id={proxy_id} type={msg.get('type')} trace_id={trace_id} task_id={task_id} bytes={len(payload)}"
            )
            await asyncio.wait_for(ws.send(payload), timeout=5.0)
            logger.info(f"op=ws.send.done proxy_id={proxy_id} type={msg.get('type')} trace_id={trace_id} task_id={task_id}")


class ManagerCore:
    def __init__(
        self,
        registry: ProxyRegistry,
        *,
        task_timeout_s: float,
        recent_ttl_s: float = 1800.0,
        recent_max: int = 10000,
    ) -> None:
        self.registry = registry
        self.task_timeout_s = float(task_timeout_s)
        self.recent_ttl_s = float(recent_ttl_s)
        self.recent_max = int(recent_max)

        self._lock = asyncio.Lock()
        self._tasks_inflight: dict[str, TaskContext] = {}
        self._recent: "OrderedDict[str, tuple[TaskContext, float]]" = OrderedDict()
        self._outbox: TelegramOutbox | None = None
        self._timeout_task: asyncio.Task | None = None
        self._waiters: dict[str, asyncio.Future[JsonDict]] = {}
        self._rpc_waiters: dict[str, asyncio.Future[JsonDict]] = {}
        self._approvals: dict[str, ApprovalContext] = {}
        self.progress_update_interval_s = 5.0

    def set_outbox(self, outbox: TelegramOutbox) -> None:
        self._outbox = outbox
        if self._timeout_task is None:
            self._timeout_task = asyncio.create_task(self._timeout_loop(), name="task_timeout_loop")

    async def register_task(self, ctx: TaskContext) -> None:
        async with self._lock:
            if ctx.progress_lines is None:
                ctx.progress_lines = []
            self._tasks_inflight[ctx.task_id] = ctx
            self._gc_recent_locked()

    async def dispatch_once(self, proxy_id: str, prompt: str, *, timeout_s: float, model: str = "", effort: str = "") -> JsonDict:
        rep = await self.appserver_call(
            proxy_id,
            "thread/start",
            # Let proxy apply its local defaults (sandbox/approvalPolicy) unless caller explicitly sets them.
            {"cwd": str(BASE_DIR), "personality": "pragmatic"},
            timeout_s=min(timeout_s, 60.0),
        )
        if not bool(rep.get("ok")):
            raise RuntimeError(f"thread/start failed: {rep.get('error')}")
        result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise RuntimeError(f"thread/start missing id: {result!r}")
        return await self.dispatch_task(proxy_id, thread_id, prompt, thread_key="probe:local", timeout_s=timeout_s, model=model, effort=effort)

    async def dispatch_task(self, proxy_id: str, thread_id: str, prompt: str, *, thread_key: str, timeout_s: float, model: str = "", effort: str = "") -> JsonDict:
        task_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        fut: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._waiters[task_id] = fut
        msg: JsonDict = {
            "type": "task_assign",
            "trace_id": trace_id,
            "task_id": task_id,
            "thread_key": thread_key,
            "thread_id": thread_id,
            "prompt": prompt,
        }
        if model:
            msg["model"] = model
        if effort:
            msg["effort"] = effort
        t0 = time.time()
        logger.info(f"op=dispatch.enqueue proxy_id={proxy_id} trace_id={trace_id} task_id={task_id} thread_id={thread_id[-8:]} prompt_len={len(prompt)}")
        await self.registry.send_json(proxy_id, msg)
        try:
            res = await asyncio.wait_for(fut, timeout=timeout_s)
            latency_ms = int((time.time() - t0) * 1000.0)
            logger.info(
                f"op=dispatch.done proxy_id={proxy_id} trace_id={trace_id} task_id={task_id} thread_id={thread_id[-8:]} "
                f"ok={bool(res.get('ok'))} latency_ms={latency_ms}"
            )
            return res
        finally:
            async with self._lock:
                self._waiters.pop(task_id, None)

    async def appserver_call(self, proxy_id: str, method: str, params: JsonDict | None, *, timeout_s: float = 30.0) -> JsonDict:
        req_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        fut: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._rpc_waiters[req_id] = fut
        msg: JsonDict = {"type": "appserver_request", "req_id": req_id, "trace_id": trace_id, "method": method, "params": params or {}}
        t0 = time.time()
        logger.info(f"op=appserver.send proxy_id={proxy_id} trace_id={trace_id} req_id={req_id} method={method}")
        try:
            await self.registry.send_json(proxy_id, msg)
            res = await asyncio.wait_for(fut, timeout=timeout_s)
            latency_ms = int((time.time() - t0) * 1000.0)
            logger.info(f"op=appserver.done proxy_id={proxy_id} trace_id={trace_id} req_id={req_id} ok={bool(res.get('ok'))} latency_ms={latency_ms}")
            return res
        finally:
            async with self._lock:
                self._rpc_waiters.pop(req_id, None)

    async def send_task_assign(self, *, proxy_id: str, task_msg: JsonDict) -> None:
        task_id = str(task_msg.get("task_id") or "")
        if not task_id:
            return
        try:
            await self.registry.send_json(proxy_id, task_msg)
        except Exception as e:
            await self._fail_task(task_id, f"ws send failed: {type(e).__name__}: {e}", proxy_id=proxy_id)

    async def on_proxy_message(self, proxy_id: str, msg: JsonDict) -> None:
        t = msg.get("type")
        if t == "approval_request":
            await self._handle_approval_request(proxy_id=proxy_id, msg=msg)
            return
        if t == "task_progress":
            await self._handle_task_progress(proxy_id=proxy_id, msg=msg)
            return
        if t == "task_ack":
            task_id = str(msg.get("task_id") or "")
            trace_id = str(msg.get("trace_id") or "")
            logger.info(f"op=ws.recv proxy_id={proxy_id} type=task_ack trace_id={trace_id} task_id={task_id}")
            return
        if t == "appserver_response":
            req_id = str(msg.get("req_id") or "")
            trace_id = str(msg.get("trace_id") or "")
            logger.info(f"op=ws.recv proxy_id={proxy_id} type=appserver_response trace_id={trace_id} req_id={req_id} ok={bool(msg.get('ok'))}")
            if not req_id:
                return
            async with self._lock:
                waiter = self._rpc_waiters.get(req_id)
                if waiter and not waiter.done():
                    waiter.set_result(msg)
                    return
            logger.info(f"orphan appserver_response: proxy={proxy_id} req_id={req_id} ok={bool(msg.get('ok'))}")
            return
        if t != "task_result":
            return
        task_id = str(msg.get("task_id") or "")
        if not task_id:
            return
        trace_id = str(msg.get("trace_id") or "")
        logger.info(f"op=ws.recv proxy_id={proxy_id} type=task_result trace_id={trace_id} task_id={task_id} ok={bool(msg.get('ok'))}")

        async with self._lock:
            waiter = self._waiters.get(task_id)
            if waiter and not waiter.done():
                waiter.set_result(msg)
                return
        await self._handle_task_result(proxy_id=proxy_id, msg=msg)

    def _push_progress_line(self, arr: list[str], text: str, limit: int = 24) -> None:
        text = (text or "").strip()
        if not text:
            return
        if arr and arr[-1] == text:
            return
        arr.append(text)
        if len(arr) > limit:
            del arr[0 : len(arr) - limit]

    def _trim_progress_line(self, text: str, limit: int = 280) -> str:
        text = (text or "").strip().replace("\n", " ")
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _normalize_progress_line(self, *, event: str, stage: str, summary: str, ctx: TaskContext, now: float) -> str | None:
        summary = self._trim_progress_line(summary)
        if not summary:
            return None
        if stage == "turn_started":
            return "已开始处理本轮请求"
        if stage == "turn_completed":
            return "本轮处理完成"
        if stage == "reasoning":
            if (now - ctx.progress_last_message_at) < 8.0:
                return None
            ctx.progress_last_message_at = now
            return "正在分析"
        if stage == "message":
            if summary == "正在生成回复":
                if (now - ctx.progress_last_message_at) < 8.0:
                    return None
                ctx.progress_last_message_at = now
                return "正在生成回复"
            # Do not include assistant message content in progress to avoid duplicating
            # the final task_result text (especially in result_mode=send). Newer proxies
            # already summarize agentMessage progress as "回复已生成(len=...)".
            if summary.startswith("回复已生成"):
                return summary
            return None
        if stage == "plan":
            return f"plan: {summary}"
        if stage == "retrying":
            return f"retry: {summary}"
        if stage == "error":
            return f"error: {summary}"
        if stage == "command":
            return summary
        if stage == "file_change":
            return summary
        return summary

    def _render_progress_text(self, ctx: TaskContext) -> str:
        head = f"working (proxy={ctx.proxy_id}, threadId={ctx.thread_id[-8:]}) ..."
        lines: list[str] = [head]
        if ctx.progress_lines:
            lines.append("")
            show = ctx.progress_lines
            if len(show) > 12:
                show = show[:4] + [f"... ({len(show) - 8} steps omitted) ..."] + show[-4:]
            lines.extend(show)
        return "\n".join(lines)

    def _render_progress_done_text(self, ctx: TaskContext, *, ok: bool) -> str:
        lines = [self._render_progress_text(ctx), ""]
        status = "working done" if ok else "working failed"
        lines.append(f"{status} (proxy={ctx.proxy_id}, threadId={ctx.thread_id[-8:]})")
        return "\n".join(lines)

    async def _handle_task_progress(self, *, proxy_id: str, msg: JsonDict) -> None:
        task_id = str(msg.get("task_id") or "")
        trace_id = str(msg.get("trace_id") or "")
        event = str(msg.get("event") or "")
        summary = str(msg.get("summary") or "").strip()
        force = bool(msg.get("force"))
        if not task_id or not summary:
            return
        logger.info(f"op=ws.recv proxy_id={proxy_id} type=task_progress trace_id={trace_id} task_id={task_id} event={event} force={force} summary={summary[:200]}")

        ctx: TaskContext | None = None
        text_to_send = ""
        async with self._lock:
            ctx = self._tasks_inflight.get(task_id)
            if ctx is None:
                return
            now = time.time()
            if ctx.progress_lines is None:
                ctx.progress_lines = []
            stage = str(msg.get("stage") or "")
            label = self._normalize_progress_line(event=event, stage=stage, summary=summary, ctx=ctx, now=now)
            if label is not None:
                self._push_progress_line(ctx.progress_lines, label)
            ctx.pending_progress_text = summary
            ctx.last_progress_event = event
            should_send = force or ctx.last_progress_at <= 0 or (now - ctx.last_progress_at) >= self.progress_update_interval_s
            if not should_send:
                return
            text_to_send = self._render_progress_text(ctx)
            ctx.last_progress_at = now
            ctx.last_progress_text = ctx.pending_progress_text
            ctx.pending_progress_text = ""

        outbox = self._outbox
        if outbox is None or ctx is None:
            return
        if not await outbox.enqueue(
            TgAction(
                type="edit",
                chat_id=ctx.chat_id,
                message_id=ctx.placeholder_msg_id,
                text=text_to_send,
                trace_id=trace_id or ctx.trace_id,
                proxy_id=proxy_id,
                task_id=task_id,
                kind="progress",
            )
        ):
            logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (progress)")

    async def _handle_approval_request(self, *, proxy_id: str, msg: JsonDict) -> None:
        approval_id = str(msg.get("approval_id") or "")
        task_id = str(msg.get("task_id") or "")
        trace_id = str(msg.get("trace_id") or "")
        method = str(msg.get("method") or "")
        params = msg.get("params")
        rpc_id = msg.get("rpc_id")
        if not approval_id or not method:
            return
        if not isinstance(params, dict):
            params = {}
        rpc_id_i = rpc_id if isinstance(rpc_id, int) else None

        ctx: TaskContext | None = None
        async with self._lock:
            # We only support approvals triggered during an inflight task for now.
            ctx = self._tasks_inflight.get(task_id) if task_id else None
        if ctx is None:
            logger.warning(f"approval_request dropped: proxy={proxy_id} approval_id={approval_id} task_id={task_id!r} method={method}")
            # Don't hang the proxy: decline immediately.
            try:
                await self.registry.send_json(proxy_id, {"type": "approval_decision", "approval_id": approval_id, "decision": "decline"})
            except Exception:
                pass
            return

        outbox = self._outbox
        if outbox is None:
            logger.warning(f"approval_request dropped (no outbox): proxy={proxy_id} approval_id={approval_id}")
            try:
                await self.registry.send_json(proxy_id, {"type": "approval_decision", "approval_id": approval_id, "decision": "decline"})
            except Exception:
                pass
            return

        ac = ApprovalContext(
            approval_id=approval_id,
            proxy_id=proxy_id,
            task_id=task_id,
            trace_id=trace_id or ctx.trace_id,
            rpc_id=rpc_id_i,
            method=method,
            params=params,
            chat_id=ctx.chat_id,
            created_at=time.time(),
        )
        async with self._lock:
            self._approvals[approval_id] = ac

        # Render a minimal approval message (align with app-server naming).
        text_lines: list[str] = []
        text_lines.append(f"[{proxy_id}] 需要审批：{method}")
        # Try to extract key fields from the official request payload.
        if method == "item/commandExecution/requestApproval":
            cmd = params.get("command")
            cwd = params.get("cwd")
            reason = params.get("reason")
            if cmd:
                text_lines.append(f"command: {cmd}")
            if cwd:
                text_lines.append(f"cwd: {cwd}")
            if reason:
                text_lines.append(f"reason: {reason}")
            net = params.get("networkApprovalContext")
            if isinstance(net, dict):
                host = net.get("host")
                proto = net.get("protocol")
                port = net.get("port")
                if host or proto or port:
                    text_lines.append(f"network: {proto or '?'} {host or '?'}:{port or '?'}")
        elif method == "item/fileChange/requestApproval":
            reason = params.get("reason")
            if reason:
                text_lines.append(f"reason: {reason}")
        text_lines.append("")
        text_lines.append("回复以下命令进行选择：")
        text_lines.append(f"/approve {approval_id}")
        text_lines.append(f"/approve_session {approval_id}")
        text_lines.append(f"/decline {approval_id}")

        await outbox.enqueue(TgAction(type="send", chat_id=ctx.chat_id, text="\n".join(text_lines), trace_id=ac.trace_id, proxy_id=proxy_id, task_id=task_id, kind="approval"))

    async def approval_decide(self, *, approval_id: str, decision: str, chat_id: int) -> tuple[bool, str]:
        decision = (decision or "").strip()
        if decision not in ("accept", "acceptForSession", "decline", "cancel"):
            return False, "invalid decision"
        async with self._lock:
            ac = self._approvals.get(approval_id)
        if ac is None:
            return False, "approval_id not found (maybe expired)"
        if ac.chat_id != chat_id:
            return False, "chat mismatch"
        try:
            await self.registry.send_json(ac.proxy_id, {"type": "approval_decision", "approval_id": approval_id, "decision": decision})
        except Exception as e:
            return False, f"send failed: {type(e).__name__}: {e}"
        async with self._lock:
            self._approvals.pop(approval_id, None)
        return True, "ok"

    async def _handle_task_result(self, *, proxy_id: str, msg: JsonDict) -> None:
        task_id = str(msg.get("task_id") or "")
        trace_id = str(msg.get("trace_id") or "")
        ok = bool(msg.get("ok"))
        text = str(msg.get("text") or "")
        err = str(msg.get("error") or "")
        if not task_id:
            return

        ctx: TaskContext | None = None
        late_ctx: TaskContext | None = None
        async with self._lock:
            ctx = self._tasks_inflight.pop(task_id, None)
            if ctx is None:
                item = self._recent.get(task_id)
                if item:
                    late_ctx = item[0]
            if ctx is not None:
                self._recent[task_id] = (ctx, time.time())
                self._recent.move_to_end(task_id)
                self._gc_recent_locked()

        outbox = self._outbox
        if outbox is None:
            logger.warning(f"task_result dropped (no outbox): proxy={proxy_id} task_id={task_id} ok={ok}")
            return

        if ctx is None and late_ctx is not None:
            body = text.strip() if ok else f"error: {err or 'unknown error'}"
            body = body or "(empty)"
            parts = _prefix_and_split_telegram_text(body, prefix=f"[{proxy_id}][late task_id={task_id}] ")
            for p in parts:
                await outbox.enqueue(
                    TgAction(type="send", chat_id=late_ctx.chat_id, text=p, trace_id=trace_id or late_ctx.trace_id, proxy_id=proxy_id, task_id=task_id, kind="late")
                )
            return

        if ctx is None:
            logger.info(f"orphan task_result: proxy={proxy_id} task_id={task_id} ok={ok} err={err!r}")
            return

        if ok:
            body = text.strip() or "(empty)"
            parts = _prefix_and_split_telegram_text(body, prefix=f"[{proxy_id}] ")
            if ctx.result_mode == "send":
                done_text = self._render_progress_done_text(ctx, ok=True)
                if not await outbox.enqueue(
                    TgAction(
                        type="edit",
                        chat_id=ctx.chat_id,
                        message_id=ctx.placeholder_msg_id,
                        text=done_text,
                        trace_id=trace_id or ctx.trace_id,
                        proxy_id=proxy_id,
                        task_id=task_id,
                        kind="result_done",
                    )
                ):
                    logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (edit done)")
                for extra in parts:
                    if not await outbox.enqueue(
                        TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=trace_id or ctx.trace_id, proxy_id=proxy_id, task_id=task_id, kind="result")
                    ):
                        logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (send result)")
            else:
                if not await outbox.enqueue(
                    TgAction(
                        type="edit",
                        chat_id=ctx.chat_id,
                        message_id=ctx.placeholder_msg_id,
                        text=parts[0],
                        trace_id=trace_id or ctx.trace_id,
                        proxy_id=proxy_id,
                        task_id=task_id,
                        kind="result",
                    )
                ):
                    logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (edit)")
                for extra in parts[1:]:
                    if not await outbox.enqueue(
                        TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=trace_id or ctx.trace_id, proxy_id=proxy_id, task_id=task_id, kind="result")
                    ):
                        logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (send extra)")
            return

        body = f"error: {err or 'unknown error'}"
        parts = _prefix_and_split_telegram_text(body, prefix=f"[{proxy_id}] ")
        if ctx.result_mode == "send":
            done_text = self._render_progress_done_text(ctx, ok=False)
            if not await outbox.enqueue(
                TgAction(
                    type="edit",
                    chat_id=ctx.chat_id,
                    message_id=ctx.placeholder_msg_id,
                    text=done_text,
                    trace_id=trace_id or ctx.trace_id,
                    proxy_id=proxy_id,
                    task_id=task_id,
                    kind="result_done",
                )
            ):
                logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (edit err done)")
            for extra in parts:
                if not await outbox.enqueue(
                    TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=trace_id or ctx.trace_id, proxy_id=proxy_id, task_id=task_id, kind="result")
                ):
                    logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (send err)")
        else:
            if not await outbox.enqueue(
                TgAction(
                    type="edit",
                    chat_id=ctx.chat_id,
                    message_id=ctx.placeholder_msg_id,
                    text=parts[0],
                    trace_id=trace_id or ctx.trace_id,
                    proxy_id=proxy_id,
                    task_id=task_id,
                    kind="result",
                )
            ):
                logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (edit err)")
            for extra in parts[1:]:
                if not await outbox.enqueue(
                    TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=trace_id or ctx.trace_id, proxy_id=proxy_id, task_id=task_id, kind="result")
                ):
                    logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (send extra err)")

    async def _fail_task(self, task_id: str, reason: str, *, proxy_id: str) -> None:
        ctx: TaskContext | None = None
        async with self._lock:
            ctx = self._tasks_inflight.pop(task_id, None)
            if ctx is not None:
                self._recent[task_id] = (ctx, time.time())
                self._recent.move_to_end(task_id)
                self._gc_recent_locked()
        outbox = self._outbox
        if outbox is None or ctx is None:
            return
        parts = _prefix_and_split_telegram_text(f"error: {reason}", prefix=f"[{proxy_id}] ")
        await outbox.enqueue(
            TgAction(
                type="edit",
                chat_id=ctx.chat_id,
                message_id=ctx.placeholder_msg_id,
                text=parts[0],
                trace_id=ctx.trace_id,
                proxy_id=proxy_id,
                task_id=task_id,
                kind="manager_error",
            )
        )
        for extra in parts[1:]:
            await outbox.enqueue(
                TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=ctx.trace_id, proxy_id=proxy_id, task_id=task_id, kind="manager_error")
            )

    async def _timeout_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            expired: list[tuple[str, TaskContext]] = []
            async with self._lock:
                for tid, ctx in list(self._tasks_inflight.items()):
                    if (now - ctx.created_at) > self.task_timeout_s:
                        expired.append((tid, ctx))
                        self._tasks_inflight.pop(tid, None)
                        self._recent[tid] = (ctx, now)
                        self._recent.move_to_end(tid)
                self._gc_recent_locked()
            if not expired:
                continue
            outbox = self._outbox
            if outbox is None:
                continue
            for _tid, ctx in expired:
                parts = _prefix_and_split_telegram_text("error: timeout", prefix=f"[{ctx.proxy_id}] ")
                await outbox.enqueue(
                    TgAction(
                        type="edit",
                        chat_id=ctx.chat_id,
                        message_id=ctx.placeholder_msg_id,
                        text=parts[0],
                        trace_id=ctx.trace_id,
                        proxy_id=ctx.proxy_id,
                        task_id=_tid,
                        kind="timeout",
                    )
                )
                for extra in parts[1:]:
                    await outbox.enqueue(
                        TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=ctx.trace_id, proxy_id=ctx.proxy_id, task_id=_tid, kind="timeout")
                    )

    def _gc_recent_locked(self) -> None:
        now = time.time()
        while self._recent:
            tid, (_ctx, ts) = next(iter(self._recent.items()))
            if (now - ts) <= self.recent_ttl_s:
                break
            self._recent.pop(tid, None)
        while len(self._recent) > self.recent_max:
            self._recent.popitem(last=False)


async def run_ws_server(listen: str, registry: ProxyRegistry, core: ManagerCore) -> None:
    host, port_s = listen.rsplit(":", 1)
    port = int(port_s)

    async def handler(ws: Any):
        proxy_id = ""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            msg = json.loads(raw)
            if not isinstance(msg, dict) or msg.get("type") != "register":
                await ws.send(json.dumps({"type": "register_error", "error": "expected register"}))
                return
            proxy_id = str(msg.get("proxy_id") or "").strip()
            token = str(msg.get("token") or "").strip()
            if not proxy_id:
                await ws.send(json.dumps({"type": "register_error", "error": "missing proxy_id"}))
                return
            ok = await registry.register(proxy_id=proxy_id, token=token, ws=ws)
            if not ok:
                await ws.send(json.dumps({"type": "register_error", "error": "unauthorized"}))
                return
            await ws.send(json.dumps({"type": "register_ok"}))
            logger.info(f"proxy online: {proxy_id}")

            async for raw in ws:
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(m, dict):
                    continue
                t = m.get("type")
                if t == "heartbeat":
                    await registry.heartbeat(proxy_id, ws)
                    continue
                if t in ("task_ack", "task_result", "task_progress", "appserver_response", "approval_request"):
                    await core.on_proxy_message(proxy_id, m)
                    continue
        except websockets.ConnectionClosed:
            return
        except Exception as e:
            logger.warning(f"ws handler error for proxy {proxy_id or '<unregistered>'}: {e}")
            return
        finally:
            if proxy_id:
                await registry.unregister_if_matches(proxy_id, ws)
                logger.info(f"proxy offline: {proxy_id}")

    async with websockets.serve(handler, host, port, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024):
        logger.info(f"WS listening on {listen}")
        await asyncio.Future()  # run forever


async def run_control_server(listen: str, token: str, registry: ProxyRegistry, core: ManagerCore) -> None:
    """
    Local control plane for phase-2 verification.
    JSON-lines over TCP, default bind should be 127.0.0.1 only.

    Request (one line):
      {"type":"servers","token":"..."}
      {"type":"dispatch","token":"...","proxy_id":"proxy27","prompt":"ping","timeout":60}
      {"type":"appserver","token":"...","proxy_id":"proxy27","method":"thread/list","params":{"limit":1},"timeout":30}
      {"type":"dispatch_text","token":"...","proxy_id":"proxy27","session_key":"tg:1:2","text":"ping","timeout":60}

    Response (one line JSON):
      {"ok":true,"type":"servers","online":["proxy1"],"allowed":["proxy1","proxy2"]}
      {"ok":true,"type":"dispatch","result":{...task_result...}}
    """
    host, port_s = listen.rsplit(":", 1)
    port = int(port_s)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except Exception:
                    writer.write((json.dumps({"ok": False, "error": "bad json"}) + "\n").encode("utf-8"))
                    await writer.drain()
                    continue
                if not isinstance(req, dict):
                    writer.write((json.dumps({"ok": False, "error": "bad request"}) + "\n").encode("utf-8"))
                    await writer.drain()
                    continue
                if str(req.get("token") or "") != token:
                    logger.warning(f"op=ctl.recv ok=false peer={peer} error=unauthorized")
                    writer.write((json.dumps({"ok": False, "error": "unauthorized"}) + "\n").encode("utf-8"))
                    await writer.drain()
                    continue
                t = str(req.get("type") or "")
                if t == "servers":
                    resp = {
                        "ok": True,
                        "type": "servers",
                        "online": registry.online_proxy_ids(),
                        "allowed": registry.allowed_proxy_ids(),
                    }
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    await writer.drain()
                    logger.info(f"op=ctl.send peer={peer} type=servers ok=true")
                    continue
                if t == "dispatch":
                    proxy_id = str(req.get("proxy_id") or "").strip()
                    prompt = str(req.get("prompt") or "ping")
                    timeout_s = float(req.get("timeout") or 60.0)
                    model = str(req.get("model") or "").strip()
                    effort = str(req.get("effort") or "").strip()
                    if not proxy_id:
                        writer.write((json.dumps({"ok": False, "type": "dispatch", "error": "missing proxy_id"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    try:
                        res = await core.dispatch_once(proxy_id, prompt, timeout_s=timeout_s, model=model, effort=effort)
                        resp = {"ok": True, "type": "dispatch", "result": res}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=dispatch ok=true proxy_id={proxy_id}")
                    except Exception as e:
                        resp = {"ok": False, "type": "dispatch", "error": f"{type(e).__name__}: {e}"}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=dispatch ok=false proxy_id={proxy_id} error={type(e).__name__}")
                    continue
                if t == "appserver":
                    proxy_id = str(req.get("proxy_id") or "").strip()
                    method = str(req.get("method") or "").strip()
                    params = req.get("params")
                    timeout_s = float(req.get("timeout") or 30.0)
                    if not proxy_id:
                        writer.write((json.dumps({"ok": False, "type": "appserver", "error": "missing proxy_id"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    if not method:
                        writer.write((json.dumps({"ok": False, "type": "appserver", "error": "missing method"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    try:
                        res = await core.appserver_call(proxy_id, method, params if isinstance(params, dict) else {}, timeout_s=timeout_s)
                        resp = {"ok": True, "type": "appserver", "result": res}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=appserver ok=true proxy_id={proxy_id} method={method}")
                    except Exception as e:
                        resp = {"ok": False, "type": "appserver", "error": f"{type(e).__name__}: {e}"}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=appserver ok=false proxy_id={proxy_id} method={method} error={type(e).__name__}")
                    continue
                if t == "dispatch_text":
                    proxy_id = str(req.get("proxy_id") or "").strip()
                    session_key = str(req.get("session_key") or "").strip()
                    text = str(req.get("text") or "")
                    timeout_s = float(req.get("timeout") or 60.0)
                    if not proxy_id or not session_key:
                        writer.write((json.dumps({"ok": False, "type": "dispatch_text", "error": "missing proxy_id/session_key"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    # Ensure thread for this (session_key, proxy_id)
                    try:
                        # Load mapping via sessions.json on disk each time (control plane only).
                        sessions = load_sessions()
                        sess = sessions.get(session_key) or {"proxy": proxy_id, "by_proxy": {}, "defaults": {}}
                        byp = sess.get("by_proxy") if isinstance(sess.get("by_proxy"), dict) else {}
                        entry = byp.get(proxy_id) if isinstance(byp.get(proxy_id), dict) else {}
                        thread_id = str(entry.get("current_thread_id") or "")
                        if not thread_id:
                            rep = await core.appserver_call(
                                proxy_id,
                                "thread/start",
                                {"cwd": str(BASE_DIR), "personality": "pragmatic"},
                                timeout_s=min(timeout_s, 60.0),
                            )
                            if not bool(rep.get("ok")):
                                raise RuntimeError(f"thread/start failed: {rep.get('error')}")
                            result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
                            thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
                            thread_id = str(thread.get("id") or "")
                            if not thread_id:
                                raise RuntimeError(f"thread/start missing id: {result!r}")
                            byp.setdefault(proxy_id, {})["current_thread_id"] = thread_id
                            sess["by_proxy"] = byp
                            sessions[session_key] = sess
                            save_sessions(sessions)
                        res = await core.dispatch_task(proxy_id, thread_id, text, thread_key=session_key, timeout_s=timeout_s)
                        writer.write((json.dumps({"ok": True, "type": "dispatch_text", "thread_id": thread_id, "result": res}, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=dispatch_text ok=true proxy_id={proxy_id} session_key={session_key}")
                    except Exception as e:
                        writer.write((json.dumps({"ok": False, "type": "dispatch_text", "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=dispatch_text ok=false proxy_id={proxy_id} error={type(e).__name__}")
                    continue
                writer.write((json.dumps({"ok": False, "error": "unknown type"}) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(_handle, host=host, port=port)
    logger.info(f"Control listening on {listen}")
    async with server:
        await server.serve_forever()


def _session_key(update: Update) -> str:
    assert update.effective_chat is not None
    assert update.effective_user is not None
    return f"tg:{update.effective_chat.id}:{update.effective_user.id}"


class ManagerApp:
    def __init__(
        self,
        core: ManagerCore,
        registry: ProxyRegistry,
        sessions: dict[str, dict],
        allowed_users: set[int],
        default_proxy: str,
        task_timeout_s: float,
    ) -> None:
        self.core = core
        self.registry = registry
        self.sessions = sessions
        self.allowed_users = allowed_users
        self.default_proxy = default_proxy
        self.task_timeout_s = task_timeout_s

    def _is_allowed(self, update: Update) -> bool:
        if not self.allowed_users:
            return True
        uid = update.effective_user.id if update.effective_user else None
        return isinstance(uid, int) and uid in self.allowed_users

    def _choose_proxy(self, session_proxy: str) -> str | None:
        if session_proxy and self.registry.is_online(session_proxy):
            return session_proxy
        if self.default_proxy and self.registry.is_online(self.default_proxy):
            return self.default_proxy
        online = self.registry.online_proxy_ids()
        return online[0] if online else None

    def _get_sess(self, sk: str) -> dict:
        sess = self.sessions.get(sk)
        if not isinstance(sess, dict):
            sess = {"proxy": "", "by_proxy": {}, "defaults": {}, "result_mode": "send"}
            self.sessions[sk] = sess
        if not isinstance(sess.get("by_proxy"), dict):
            sess["by_proxy"] = {}
        if not isinstance(sess.get("defaults"), dict):
            sess["defaults"] = {}
        if str(sess.get("result_mode") or "").strip() not in ("replace", "send"):
            sess["result_mode"] = "send"
        return sess

    def _get_selected_proxy(self, sk: str) -> str:
        sess = self._get_sess(sk)
        return str(sess.get("proxy") or "").strip()

    def _get_result_mode(self, sk: str) -> str:
        sess = self._get_sess(sk)
        mode = str(sess.get("result_mode") or "send").strip()
        aliases = {"replace": "replace", "edit": "replace", "send": "send", "separate": "send"}
        return aliases.get(mode, "send")

    def _set_result_mode(self, sk: str, mode: str) -> None:
        mode = (mode or "").strip()
        aliases = {"replace": "replace", "edit": "replace", "send": "send", "separate": "send"}
        mode = aliases.get(mode, "send")
        sess = self._get_sess(sk)
        sess["result_mode"] = mode
        self.sessions[sk] = sess

    def _set_selected_proxy(self, sk: str, proxy_id: str) -> None:
        sess = self._get_sess(sk)
        sess["proxy"] = str(proxy_id or "").strip()
        self.sessions[sk] = sess

    def _get_current_thread_id(self, sk: str, proxy_id: str) -> str:
        sess = self._get_sess(sk)
        byp = sess["by_proxy"]
        entry = byp.get(proxy_id)
        if not isinstance(entry, dict):
            return ""
        return str(entry.get("current_thread_id") or "").strip()

    def _get_defaults(self, sk: str) -> dict[str, Any]:
        sess = self._get_sess(sk)
        defaults = sess.get("defaults")
        return defaults if isinstance(defaults, dict) else {}

    def _get_default_model(self, sk: str) -> str:
        defaults = self._get_defaults(sk)
        return str(defaults.get("model") or "").strip()

    def _set_default_model(self, sk: str, model: str) -> None:
        sess = self._get_sess(sk)
        defaults = sess.get("defaults")
        if not isinstance(defaults, dict):
            defaults = {}
        model = str(model or "").strip()
        if model:
            defaults["model"] = model
        else:
            defaults.pop("model", None)
        sess["defaults"] = defaults
        self.sessions[sk] = sess

    def _set_current_thread_id(self, sk: str, proxy_id: str, thread_id: str) -> None:
        sess = self._get_sess(sk)
        byp = sess["by_proxy"]
        entry = byp.get(proxy_id)
        if not isinstance(entry, dict):
            entry = {}
            byp[proxy_id] = entry
        entry["current_thread_id"] = str(thread_id or "")
        entry["last_used_at"] = int(time.time())
        sess["by_proxy"] = byp
        self.sessions[sk] = sess

    def _parse_kv(self, args: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for a in args:
            if "=" not in a:
                continue
            k, v = a.split("=", 1)
            k = k.strip()
            v = v.strip()
            if not k:
                continue
            out[k] = v
        return out

    def _pretty_json(self, obj: Any, *, limit: int = 3000) -> str:
        s = json.dumps(obj, ensure_ascii=False, indent=2)
        if len(s) <= limit:
            return s
        return s[:limit] + f"\n...(truncated, total={len(s)})"

    def _fmt_unix_ts(self, value: Any) -> str:
        try:
            ts = float(value)
        except Exception:
            return "(unknown)"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _append_rate_limit_lines(self, lines: list[str], rate_result: dict[str, Any]) -> None:
        rl_map = rate_result.get("rateLimitsByLimitId") if isinstance(rate_result.get("rateLimitsByLimitId"), dict) else {}
        buckets: list[tuple[str, dict[str, Any]]] = []
        for limit_id, payload in rl_map.items():
            if isinstance(limit_id, str) and isinstance(payload, dict):
                buckets.append((limit_id, payload))
        if not buckets:
            payload = rate_result.get("rateLimits") if isinstance(rate_result.get("rateLimits"), dict) else {}
            if payload:
                buckets.append(("default", payload))
        if not buckets:
            lines.append("rate_limits: (unavailable)")
            return
        for limit_id, payload in buckets:
            primary = payload.get("primary") if isinstance(payload.get("primary"), dict) else {}
            secondary = payload.get("secondary") if isinstance(payload.get("secondary"), dict) else {}
            plan_type = str(payload.get("planType") or "").strip()
            if plan_type:
                lines.append(f"rate_limit[{limit_id}].plan: {plan_type}")
            for name, bucket in (("primary", primary), ("secondary", secondary)):
                if not bucket:
                    continue
                used_percent = bucket.get("usedPercent")
                remaining_percent = "(unknown)"
                if isinstance(used_percent, (int, float)):
                    remaining_percent = f"{max(0.0, 100.0 - float(used_percent)):.0f}%"
                window_mins = bucket.get("windowDurationMins")
                resets_at = self._fmt_unix_ts(bucket.get("resetsAt"))
                label = name
                if window_mins == 300:
                    label = "5h"
                elif window_mins == 10080:
                    label = "weekly"
                lines.append(f"rate_limit[{limit_id}].{label}: remaining={remaining_percent}, window={window_mins or '(unknown)'}m, resets_at={resets_at}")

    async def _fetch_model_state(self, *, proxy_id: str, core: ManagerCore, session_model: str) -> tuple[str, str, list[str]]:
        cfg_rep = await core.appserver_call(proxy_id, "config/read", {}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(cfg_rep.get("ok")):
            raise RuntimeError(str(cfg_rep.get("error") or "config/read failed"))
        cfg_result = cfg_rep.get("result") if isinstance(cfg_rep.get("result"), dict) else {}
        cfg_obj = cfg_result.get("config") if isinstance(cfg_result.get("config"), dict) else {}
        proxy_default_model = str(cfg_obj.get("model") or "").strip()
        effective_model = session_model or proxy_default_model

        rep = await core.appserver_call(proxy_id, "model/list", {}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            raise RuntimeError(str(rep.get("error") or "model/list failed"))
        result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
        data = result.get("data") if isinstance(result.get("data"), list) else []
        models: list[str] = []
        for item in data[:30]:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or "").strip()
            if mid:
                models.append(mid)
        return effective_model, proxy_default_model, models

    def _render_model_text(self, *, proxy_id: str, effective_model: str, proxy_default_model: str, session_model: str, models: list[str]) -> str:
        lines = [f"model: {effective_model or '(unknown)'}", f"proxy: {proxy_id}"]
        if session_model:
            lines.append("source: session override")
        elif proxy_default_model:
            lines.append("source: proxy default")
        # Keep the text compact: available models are selectable via buttons.
        if models:
            shown = min(len(models), 12)
            lines.append(f"available: {shown} shown in buttons (total={len(models)})")
        else:
            lines.append("available: (none)")
        return "\n".join(lines)

    def _build_model_keyboard(self, *, models: list[str], effective_model: str, session_model: str) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for mid in models[:12]:
            label = f"• {mid}" if mid == effective_model else mid
            row.append(InlineKeyboardButton(label[:30], callback_data=f"model:set:{mid}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([
            InlineKeyboardButton("Clear", callback_data="model:clear"),
            InlineKeyboardButton("Refresh", callback_data="model:refresh"),
        ])
        return InlineKeyboardMarkup(rows)

    async def cmd_proxy_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /proxy_list chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/proxy_list reply")
            return
        sk = _session_key(update)
        selected = self._get_selected_proxy(sk)
        online = self.registry.online_proxy_ids()
        allowed = self.registry.allowed_proxy_ids()
        lines = []
        lines.append(f"online: {', '.join(online) if online else '(none)'}")
        lines.append(f"selected: {selected or '(none)'}")
        if allowed:
            lines.append(f"allowed: {', '.join(allowed)}")
        if online:
            lines.append("")
            lines.append("可直接复制切换：")
            for pid in online:
                lines.append(f"/proxy_use {pid}")
        else:
            lines.append("use: /proxy_use <id>")
        await _tg_call(update.message.reply_text("\n".join(lines)), timeout_s=15.0, what="/proxy_list reply")

    async def cmd_proxy_use(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /proxy_use chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/proxy_use reply")
            return
        sk = _session_key(update)
        # Keep /proxy_use strict: only accept a single positional proxy id.
        # (No proxyId=... form; avoid redundant syntax.)
        proxy_id = (context.args[0] if context.args else "").strip()
        if not proxy_id:
            await _tg_call(update.message.reply_text("usage: /proxy_use <id>"), timeout_s=15.0, what="/proxy_use reply")
            return
        if not self.registry.is_online(proxy_id):
            await _tg_call(update.message.reply_text(f"proxy offline: {proxy_id}"), timeout_s=15.0, what="/proxy_use reply")
            return
        self._set_selected_proxy(sk, proxy_id)
        save_sessions(self.sessions)
        await _tg_call(update.message.reply_text(f"ok proxy={proxy_id}"), timeout_s=15.0, what="/proxy_use reply")

    async def cmd_proxy_current(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /proxy_current chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/proxy_current reply")
            return
        sk = _session_key(update)
        proxy_id = self._get_selected_proxy(sk)
        await _tg_call(update.message.reply_text(f"proxy: {proxy_id or '(none)'}"), timeout_s=15.0, what="/proxy_current reply")

    async def cmd_result_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /result_mode chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/result_mode reply")
            return
        sk = _session_key(update)
        if not context.args:
            mode = self._get_result_mode(sk)
            await _tg_call(update.message.reply_text(f"result_mode: {mode}"), timeout_s=15.0, what="/result_mode reply")
            return
        mode = (context.args[0] or "").strip().lower()
        aliases = {"replace": "replace", "edit": "replace", "send": "send", "separate": "send"}
        mode = aliases.get(mode, "")
        if not mode:
            await _tg_call(update.message.reply_text("usage: /result_mode <replace|send>"), timeout_s=15.0, what="/result_mode reply")
            return
        self._set_result_mode(sk, mode)
        save_sessions(self.sessions)
        await _tg_call(update.message.reply_text(f"ok result_mode={mode}"), timeout_s=15.0, what="/result_mode reply")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /status chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/status reply")
            return
        sk = _session_key(update)
        proxy_id = self._get_selected_proxy(sk)
        if not proxy_id:
            await _tg_call(update.message.reply_text("请先 /proxy_list 查看在线代理，然后 /proxy_use <id> 选择一台机器"), timeout_s=15.0, what="/status reply")
            return
        if not self.registry.is_online(proxy_id):
            await _tg_call(update.message.reply_text(f"proxy offline: {proxy_id} (use /proxy_list)"), timeout_s=15.0, what="/status reply")
            return
        core = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/status reply")
            return
        session_model = self._get_default_model(sk)
        current_thread_id = self._get_current_thread_id(sk, proxy_id)
        result_mode = self._get_result_mode(sk)

        cfg_rep = await core.appserver_call(proxy_id, "config/read", {}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(cfg_rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {cfg_rep.get('error')}"), timeout_s=15.0, what="/status reply")
            return
        cfg_result = cfg_rep.get("result") if isinstance(cfg_rep.get("result"), dict) else {}
        cfg_obj = cfg_result.get("config") if isinstance(cfg_result.get("config"), dict) else {}
        account_rep = await core.appserver_call(proxy_id, "account/read", {}, timeout_s=min(60.0, self.task_timeout_s))
        rate_limits_rep = await core.appserver_call(proxy_id, "account/rateLimits/read", {}, timeout_s=min(60.0, self.task_timeout_s))

        loaded_rep = await core.appserver_call(proxy_id, "thread/loaded/list", {}, timeout_s=min(60.0, self.task_timeout_s))
        loaded_data: list[str] = []
        if bool(loaded_rep.get("ok")):
            loaded_result = loaded_rep.get("result") if isinstance(loaded_rep.get("result"), dict) else {}
            loaded_data = loaded_result.get("data") if isinstance(loaded_result.get("data"), list) else []

        thread_list_rep = await core.appserver_call(proxy_id, "thread/list", {"limit": 5}, timeout_s=min(60.0, self.task_timeout_s))
        thread_items: list[dict[str, Any]] = []
        if bool(thread_list_rep.get("ok")):
            tl_result = thread_list_rep.get("result") if isinstance(thread_list_rep.get("result"), dict) else {}
            thread_items = tl_result.get("data") if isinstance(tl_result.get("data"), list) else []

        collab_rep = await core.appserver_call(proxy_id, "collaborationMode/list", {}, timeout_s=min(60.0, self.task_timeout_s))
        collab_modes: list[str] = []
        if bool(collab_rep.get("ok")):
            cm_result = collab_rep.get("result") if isinstance(collab_rep.get("result"), dict) else {}
            cm_data = cm_result.get("data") if isinstance(cm_result.get("data"), list) else []
            for item in cm_data:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("mode") or "").strip()
                    if name:
                        collab_modes.append(name)

        appserver_config_model = str(cfg_obj.get("model") or "").strip()
        next_turn_model = session_model or appserver_config_model
        lines: list[str] = []
        lines.append(f"proxy: {proxy_id}")
        # IMPORTANT:
        # - app-server `config/read` is the only reliable "raw" source we have for model right now.
        #   `thread/read` (current codex versions) does not include a `model` field on the thread object.
        # - Telegram /model is a manager-side preference that we pass as per-turn override on `turn/start`.
        lines.append(f"appserver_config_model: {appserver_config_model or '(unknown)'}")
        lines.append(f"session_model_pref: {session_model or '(none)'}")
        lines.append(f"next_turn_model: {next_turn_model or '(unknown)'}")
        lines.append(f"model_reasoning_effort: {str(cfg_obj.get('model_reasoning_effort') or '(unknown)')}")
        lines.append(f"model_reasoning_summary: {str(cfg_obj.get('model_reasoning_summary') or '(unknown)')}")
        lines.append(f"directory: {str((thread_items[0].get('cwd') if thread_items and isinstance(thread_items[0], dict) else '') or BASE_DIR)}")
        lines.append(f"permissions: sandbox={str(cfg_obj.get('sandbox_mode') or '(unknown)')}, approval={str(cfg_obj.get('approval_policy') or '(unknown)')}")
        lines.append(f"agents_md: {'AGENTS.md' if (BASE_DIR / 'AGENTS.md').exists() else '(none)'}")
        lines.append(f"collaboration_modes: {', '.join(collab_modes) if collab_modes else '(unknown)'}")
        lines.append(f"result_mode: {result_mode}")
        lines.append(f"session_thread: {current_thread_id or '(none)'}")
        if bool(account_rep.get("ok")):
            account_result = account_rep.get("result") if isinstance(account_rep.get("result"), dict) else {}
            account_obj = account_result.get("account") if isinstance(account_result.get("account"), dict) else {}
            lines.append(f"account_type: {str(account_obj.get('type') or '(unknown)')}")
            email = str(account_obj.get("email") or "").strip()
            if email:
                lines.append(f"account_email: {email}")
            plan_type = str(account_obj.get("planType") or "").strip()
            if plan_type:
                lines.append(f"account_plan: {plan_type}")
        else:
            lines.append(f"account_error: {str(account_rep.get('error') or '(unknown)')}")
        if bool(rate_limits_rep.get("ok")):
            rate_result = rate_limits_rep.get("result") if isinstance(rate_limits_rep.get("result"), dict) else {}
            self._append_rate_limit_lines(lines, rate_result)
        else:
            lines.append(f"rate_limits_error: {str(rate_limits_rep.get('error') or '(unknown)')}")
        if current_thread_id:
            lines.append(f"thread_loaded: {'yes' if current_thread_id in loaded_data else 'no'}")

        if thread_items:
            current_item = None
            if current_thread_id:
                for item in thread_items:
                    if isinstance(item, dict) and str(item.get('id') or '') == current_thread_id:
                        current_item = item
                        break
            if current_item is None and thread_items and isinstance(thread_items[0], dict):
                current_item = thread_items[0]
            if isinstance(current_item, dict):
                lines.append(f"thread_status: {str((current_item.get('status') or {}).get('type') if isinstance(current_item.get('status'), dict) else '(unknown)')}")
                lines.append(f"thread_updated_at: {str(current_item.get('updatedAt') or '(unknown)')}")
                preview = str(current_item.get("preview") or "").strip()
                if preview:
                    lines.append(f"thread_preview: {preview[:160]}")
                cli_ver = str(current_item.get("cliVersion") or "").strip()
                if cli_ver:
                    lines.append(f"cli_version: {cli_ver}")
                git_info = current_item.get("gitInfo") if isinstance(current_item.get("gitInfo"), dict) else {}
                branch = str(git_info.get("branch") or "").strip()
                sha = str(git_info.get("sha") or "").strip()
                if branch or sha:
                    lines.append(f"git: branch={branch or '?'} sha={sha[:12] if sha else '?'}")

        lines.append(f"loaded_threads: {len(loaded_data)}")
        await _tg_call(update.message.reply_text("\n".join(lines)), timeout_s=15.0, what="/status reply")

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /model chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/model reply")
            return
        sk = _session_key(update)
        session_model = self._get_default_model(sk)
        if not context.args:
            proxy_id = await self._require_proxy_online(update)
            if not proxy_id:
                return
            core: ManagerCore | None = context.application.bot_data.get("core")
            if core is None:
                await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/model reply")
                return
            try:
                effective_model, proxy_default_model, models = await self._fetch_model_state(proxy_id=proxy_id, core=core, session_model=session_model)
            except Exception as e:
                await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {type(e).__name__}: {e}"), timeout_s=15.0, what="/model reply")
                return
            text = self._render_model_text(proxy_id=proxy_id, effective_model=effective_model, proxy_default_model=proxy_default_model, session_model=session_model, models=models)
            kb = self._build_model_keyboard(models=models, effective_model=effective_model, session_model=session_model)
            await _tg_call(update.message.reply_text(text, reply_markup=kb), timeout_s=15.0, what="/model reply")
            return
        arg = " ".join(context.args).strip()
        if arg.lower() in ("clear", "default", "reset"):
            self._set_default_model(sk, "")
            save_sessions(self.sessions)
            await _tg_call(update.message.reply_text("ok model=(default)"), timeout_s=15.0, what="/model reply")
            return
        self._set_default_model(sk, arg)
        save_sessions(self.sessions)
        await _tg_call(update.message.reply_text(f"ok model={arg}"), timeout_s=15.0, what="/model reply")

    async def on_model_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if not self._is_allowed(update):
            await q.answer("unauthorized", show_alert=True)
            return
        data = str(q.data or "")
        if not data.startswith("model:"):
            return
        sk = _session_key(update)
        proxy_id = self._get_selected_proxy(sk)
        if not proxy_id or not self.registry.is_online(proxy_id):
            await q.answer("proxy offline", show_alert=True)
            return
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await q.answer("manager core missing", show_alert=True)
            return
        if data == "model:clear":
            self._set_default_model(sk, "")
            save_sessions(self.sessions)
        elif data == "model:refresh":
            pass
        elif data.startswith("model:set:"):
            model_id = data[len("model:set:") :].strip()
            if not model_id:
                await q.answer("bad model id", show_alert=True)
                return
            self._set_default_model(sk, model_id)
            save_sessions(self.sessions)
        else:
            await q.answer()
            return

        session_model = self._get_default_model(sk)
        try:
            effective_model, proxy_default_model, models = await self._fetch_model_state(proxy_id=proxy_id, core=core, session_model=session_model)
        except Exception as e:
            await q.answer(f"{type(e).__name__}: {e}", show_alert=True)
            return
        text = self._render_model_text(proxy_id=proxy_id, effective_model=effective_model, proxy_default_model=proxy_default_model, session_model=session_model, models=models)
        kb = self._build_model_keyboard(models=models, effective_model=effective_model, session_model=session_model)
        await _tg_call(q.edit_message_text(text=text, reply_markup=kb), timeout_s=15.0, what="model callback edit")
        await q.answer(f"model={session_model or proxy_default_model or '(default)'}")

    async def cmd_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(
            f"cmd /ping from chat={update.effective_chat.id if update.effective_chat else '?'} "
            f"user={update.effective_user.id if update.effective_user else '?'}"
        )
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/ping reply")
            return
        await _tg_call(update.message.reply_text("pong"), timeout_s=15.0, what="/ping reply")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /help chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/help reply")
            return

        lines: list[str] = []
        lines.append("Codex Manager (TG -> Manager -> Proxy -> Codex app-server)")
        lines.append("")
        lines.append("1) 选择机器（proxy）")
        lines.append("- /proxy_list  查看在线机器（旧命令: /servers）")
        lines.append("- /proxy_use <id>  选择机器（旧命令: /use <id>）")
        lines.append("- /proxy_current  查看当前选择")
        lines.append("- /status  查看当前会话状态汇总")
        lines.append("- /model  查看当前会话模型（以及 proxy 默认模型），并列出可点按钮切换")
        lines.append("- /model <model_id>  切换当前会话模型（会在每次 turn/start 里下发，按 app-server 语义写回 thread 默认）")
        lines.append("- /result_mode [replace|send]  结果输出模式：覆盖占位 / 单独发结果")
        lines.append("")
        lines.append("2) 日常对话（turn）")
        lines.append("- 直接发送文本即可。")
        lines.append("- 若当前 proxy 没有 thread，会自动 thread/start。")
        lines.append(f"- 运行中会用 placeholder 追加进度日志，默认约每 {int(self.core.progress_update_interval_s) if hasattr(self, 'core') else 5} 秒最多更新一次。")
        lines.append("")
        lines.append("3) Thread 会话（对齐 app-server method）")
        lines.append("- /thread_current  显示当前 threadId（按 proxy 隔离保存）")
        lines.append("- /thread_start [cwd=...] [sandbox=workspaceWrite] [approvalPolicy=onRequest] [personality=pragmatic]")
        lines.append("- /thread_resume <id>  (也支持: /thread_resume threadId=<id>)")
        lines.append("- /thread_list [limit=5] [archived=true|false] [cursor=...] [sortKey=created_at|updated_at]")
        lines.append("- /thread_read <id> [includeTurns=true|false]  (也支持: threadId=<id>)")
        lines.append("- /thread_archive [threadId=<id>]   (不填则归档当前 thread)")
        lines.append("- /thread_unarchive <id>  (也支持: threadId=<id>)")
        lines.append("")
        lines.append("4) 其它 app-server 查询/配置")
        lines.append("- /model")
        lines.append("- /model <model_id>")
        lines.append("- /skills_list [cwds=/a,/b] [forceReload=true|false]")
        lines.append("- /config_read [includeLayers=true|false]")
        lines.append("- /config_value_write keyPath=<...> value=<json> [mergeStrategy=replace|upsert]")
        lines.append("- /collaborationmode_list")
        lines.append("")
        lines.append("5) 审批（approval）")
        lines.append("- 当 approvalPolicy=onRequest 时，某些命令/文件变更会触发审批。")
        lines.append("- /approve <approval_id>  同意一次")
        lines.append("- /approve_session <approval_id>  本会话同意（如果 codex 支持）")
        lines.append("- /decline <approval_id>  拒绝")
        lines.append("")
        lines.append("参数格式：key=value（多个参数用空格分隔）。JSON 参数用 value=<json>。")
        lines.append("示例：")
        lines.append("- /proxy_use proxy27")
        lines.append("- /model gpt-5-codex")
        lines.append("- /result_mode send")
        lines.append("- /thread_list limit=3 archived=false")
        lines.append("- /config_value_write keyPath=apps._default.enabled value=true mergeStrategy=replace")
        lines.append("")
        lines.append("提示：thread 内容由 Codex 保存在 proxy 机器的 ~/.codex/；manager 只保存 chat->(proxy, threadId) 路由。")

        await _tg_call(update.message.reply_text("\n".join(lines)), timeout_s=15.0, what="/help reply")

    async def cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        logger.info(f"cmd /approve chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/approve reply")
            return
        approval_id = (context.args[0] if context.args else "").strip()
        if not approval_id:
            await _tg_call(update.message.reply_text("usage: /approve <approval_id>"), timeout_s=15.0, what="/approve reply")
            return
        ok, msg = await self.core.approval_decide(approval_id=approval_id, decision="accept", chat_id=update.effective_chat.id)
        await _tg_call(update.message.reply_text(msg if ok else f"error: {msg}"), timeout_s=15.0, what="/approve reply")

    async def cmd_approve_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        logger.info(f"cmd /approve_session chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/approve_session reply")
            return
        approval_id = (context.args[0] if context.args else "").strip()
        if not approval_id:
            await _tg_call(update.message.reply_text("usage: /approve_session <approval_id>"), timeout_s=15.0, what="/approve_session reply")
            return
        ok, msg = await self.core.approval_decide(approval_id=approval_id, decision="acceptForSession", chat_id=update.effective_chat.id)
        await _tg_call(update.message.reply_text(msg if ok else f"error: {msg}"), timeout_s=15.0, what="/approve_session reply")

    async def cmd_decline(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        logger.info(f"cmd /decline chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/decline reply")
            return
        approval_id = (context.args[0] if context.args else "").strip()
        if not approval_id:
            await _tg_call(update.message.reply_text("usage: /decline <approval_id>"), timeout_s=15.0, what="/decline reply")
            return
        ok, msg = await self.core.approval_decide(approval_id=approval_id, decision="decline", chat_id=update.effective_chat.id)
        await _tg_call(update.message.reply_text(msg if ok else f"error: {msg}"), timeout_s=15.0, what="/decline reply")

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Telegram common entrypoint.
        await self.cmd_help(update, context)

    async def _require_proxy_online(self, update: Update) -> str | None:
        if not update.message:
            return None
        sk = _session_key(update)
        proxy_id = self._get_selected_proxy(sk)
        if not proxy_id:
            await _tg_call(update.message.reply_text("请先 /proxy_list 查看在线代理，然后 /proxy_use <id> 选择一台机器"), timeout_s=15.0, what="require proxy")
            return None
        if not self.registry.is_online(proxy_id):
            await _tg_call(update.message.reply_text(f"proxy offline: {proxy_id} (use /proxy_list)"), timeout_s=15.0, what="require proxy")
            return None
        return proxy_id

    async def cmd_thread_current(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /thread_current chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_current reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        sk = _session_key(update)
        tid = self._get_current_thread_id(sk, proxy_id)
        await _tg_call(update.message.reply_text(f"threadId: {tid or '(none)'}"), timeout_s=15.0, what="/thread_current reply")

    async def cmd_thread_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /thread_start chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_start reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        kv = self._parse_kv(context.args or [])
        params: JsonDict = {}
        for k in ("cwd", "sandbox", "approvalPolicy", "personality", "model", "baseInstructions"):
            if k in kv:
                params[k] = kv[k]
        sk = _session_key(update)
        if "model" not in params:
            default_model = self._get_default_model(sk)
            if default_model:
                params["model"] = default_model
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/thread_start reply")
            return
        rep = await core.appserver_call(proxy_id, "thread/start", params, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_start reply")
            return
        result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: thread/start missing id"), timeout_s=15.0, what="/thread_start reply")
            return
        self._set_current_thread_id(sk, proxy_id, thread_id)
        save_sessions(self.sessions)
        await _tg_call(update.message.reply_text(f"[{proxy_id}] ok threadId={thread_id}"), timeout_s=15.0, what="/thread_start reply")

    async def cmd_thread_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /thread_resume chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_resume reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        kv = self._parse_kv(context.args or [])
        thread_id = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        if not thread_id:
            await _tg_call(update.message.reply_text("usage: /thread_resume <id>"), timeout_s=15.0, what="/thread_resume reply")
            return
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/thread_resume reply")
            return
        rep = await core.appserver_call(proxy_id, "thread/resume", {"threadId": thread_id}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_resume reply")
            return
        sk = _session_key(update)
        self._set_current_thread_id(sk, proxy_id, thread_id)
        save_sessions(self.sessions)
        await _tg_call(update.message.reply_text(f"[{proxy_id}] ok threadId={thread_id}"), timeout_s=15.0, what="/thread_resume reply")

    async def cmd_thread_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /thread_list chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_list reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        kv = self._parse_kv(context.args or [])
        params: JsonDict = {}
        for k in ("cursor", "limit", "sortKey", "archived", "cwd", "modelProviders", "sourceKinds"):
            if k in kv:
                v: Any = kv[k]
                if k in ("limit",):
                    try:
                        v = int(v)
                    except Exception:
                        pass
                if k in ("archived",):
                    v = str(v).lower() in ("1", "true", "yes", "y", "on")
                if k in ("modelProviders", "sourceKinds"):
                    v = [x for x in str(v).split(",") if x]
                params[k] = v
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/thread_list reply")
            return
        rep = await core.appserver_call(proxy_id, "thread/list", params, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_list reply")
            return
        result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
        data = result.get("data") if isinstance(result.get("data"), list) else []
        lines: list[str] = [f"proxy: {proxy_id}", f"count: {len(data)}"]
        for i, item in enumerate(data[:20], start=1):
            if not isinstance(item, dict):
                continue
            tid = str(item.get("id") or "")
            preview = str(item.get("preview") or "")
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            stype = str(status.get("type") or "")
            lines.append(f"{i}. {tid} [{stype}] {preview[:80]}")
        if result.get("nextCursor"):
            lines.append(f"nextCursor: {result.get('nextCursor')}")
        await _tg_call(update.message.reply_text("\n".join(lines)), timeout_s=15.0, what="/thread_list reply")

    async def cmd_thread_read(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /thread_read chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_read reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        kv = self._parse_kv(context.args or [])
        thread_id = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        if not thread_id:
            await _tg_call(update.message.reply_text("usage: /thread_read <id> includeTurns=false"), timeout_s=15.0, what="/thread_read reply")
            return
        include_turns = str(kv.get("includeTurns") or "false").lower() in ("1", "true", "yes", "y", "on")
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/thread_read reply")
            return
        rep = await core.appserver_call(proxy_id, "thread/read", {"threadId": thread_id, "includeTurns": include_turns}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_read reply")
            return
        await _tg_call(update.message.reply_text(self._pretty_json(rep.get("result"))), timeout_s=15.0, what="/thread_read reply")

    async def cmd_thread_archive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /thread_archive chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_archive reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        kv = self._parse_kv(context.args or [])
        thread_id = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        if not thread_id:
            sk = _session_key(update)
            thread_id = self._get_current_thread_id(sk, proxy_id)
        if not thread_id:
            await _tg_call(update.message.reply_text("usage: /thread_archive threadId=<id> (or set current thread first)"), timeout_s=15.0, what="/thread_archive reply")
            return
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/thread_archive reply")
            return
        rep = await core.appserver_call(proxy_id, "thread/archive", {"threadId": thread_id}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_archive reply")
            return
        await _tg_call(update.message.reply_text(f"[{proxy_id}] ok archived threadId={thread_id}"), timeout_s=15.0, what="/thread_archive reply")

    async def cmd_thread_unarchive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /thread_unarchive chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_unarchive reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        kv = self._parse_kv(context.args or [])
        thread_id = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        if not thread_id:
            await _tg_call(update.message.reply_text("usage: /thread_unarchive <id>"), timeout_s=15.0, what="/thread_unarchive reply")
            return
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/thread_unarchive reply")
            return
        rep = await core.appserver_call(proxy_id, "thread/unarchive", {"threadId": thread_id}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_unarchive reply")
            return
        await _tg_call(update.message.reply_text(f"[{proxy_id}] ok unarchived threadId={thread_id}"), timeout_s=15.0, what="/thread_unarchive reply")

    async def cmd_skills_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /skills_list chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/skills_list reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        kv = self._parse_kv(context.args or [])
        params: JsonDict = {}
        if "cwds" in kv:
            params["cwds"] = [x for x in str(kv["cwds"]).split(",") if x]
        if "forceReload" in kv:
            params["forceReload"] = str(kv["forceReload"]).lower() in ("1", "true", "yes", "y", "on")
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/skills_list reply")
            return
        rep = await core.appserver_call(proxy_id, "skills/list", params, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/skills_list reply")
            return
        await _tg_call(update.message.reply_text(self._pretty_json(rep.get("result"))), timeout_s=15.0, what="/skills_list reply")

    async def cmd_config_read(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /config_read chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/config_read reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        kv = self._parse_kv(context.args or [])
        params: JsonDict = {}
        if "includeLayers" in kv:
            params["includeLayers"] = str(kv["includeLayers"]).lower() in ("1", "true", "yes", "y", "on")
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/config_read reply")
            return
        rep = await core.appserver_call(proxy_id, "config/read", params, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/config_read reply")
            return
        await _tg_call(update.message.reply_text(self._pretty_json(rep.get("result"))), timeout_s=15.0, what="/config_read reply")

    async def cmd_config_value_write(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /config_value_write chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/config_value_write reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        kv = self._parse_kv(context.args or [])
        key_path = (kv.get("keyPath") or "").strip()
        if not key_path or "value" not in kv:
            await _tg_call(update.message.reply_text("usage: /config_value_write keyPath=<...> value=<json> [mergeStrategy=replace|upsert]"), timeout_s=15.0, what="/config_value_write reply")
            return
        try:
            value_obj = json.loads(kv["value"])
        except Exception as e:
            await _tg_call(update.message.reply_text(f"bad value json: {type(e).__name__}: {e}"), timeout_s=15.0, what="/config_value_write reply")
            return
        params: JsonDict = {"keyPath": key_path, "value": value_obj}
        if "mergeStrategy" in kv:
            params["mergeStrategy"] = kv["mergeStrategy"]
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/config_value_write reply")
            return
        rep = await core.appserver_call(proxy_id, "config/value/write", params, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/config_value_write reply")
            return
        await _tg_call(update.message.reply_text(f"[{proxy_id}] ok"), timeout_s=15.0, what="/config_value_write reply")

    async def cmd_collaborationmode_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /collaborationmode_list chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/collaborationmode_list reply")
            return
        proxy_id = await self._require_proxy_online(update)
        if not proxy_id:
            return
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="/collaborationmode_list reply")
            return
        rep = await core.appserver_call(proxy_id, "collaborationMode/list", {}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/collaborationmode_list reply")
            return
        await _tg_call(update.message.reply_text(self._pretty_json(rep.get("result"))), timeout_s=15.0, what="/collaborationmode_list reply")

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        trace_id = uuid.uuid4().hex
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        text_len = len(update.message.text) if update.message and update.message.text else 0
        logger.info(f"op=tg.update trace_id={trace_id} chat_id={chat_id} user_id={user_id} text_len={text_len}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="msg reply")
            return
        if not update.message or not isinstance(update.message.text, str):
            return

        sk = _session_key(update)
        proxy_id = self._get_selected_proxy(sk)
        if not proxy_id:
            await _tg_call(update.message.reply_text("请先 /proxy_list 查看在线代理，然后 /proxy_use <id> 选择一台机器"), timeout_s=15.0, what="msg reply")
            return
        if not self.registry.is_online(proxy_id):
            await _tg_call(update.message.reply_text(f"proxy offline: {proxy_id} (use /proxy_list)"), timeout_s=15.0, what="msg reply")
            return

        task_id = uuid.uuid4().hex
        thread_id = self._get_current_thread_id(sk, proxy_id)
        core = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="msg reply")
            return
        if not thread_id:
            # Auto-create a thread on first message for this (chat, proxy).
            params: JsonDict = {"cwd": str(BASE_DIR), "personality": "pragmatic"}
            default_model = self._get_default_model(sk)
            if default_model:
                params["model"] = default_model
            rep = await core.appserver_call(proxy_id, "thread/start", params, timeout_s=min(60.0, self.task_timeout_s))
            if not bool(rep.get("ok")):
                await _tg_call(update.message.reply_text(f"[{proxy_id}] error: thread/start failed: {rep.get('error')}"), timeout_s=15.0, what="msg reply")
                return
            result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
            thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                await _tg_call(update.message.reply_text(f"[{proxy_id}] error: thread/start missing id"), timeout_s=15.0, what="msg reply")
                return
            self._set_current_thread_id(sk, proxy_id, thread_id)
            save_sessions(self.sessions)

        prompt = update.message.text
        placeholder = await _tg_call(
            update.message.reply_text(f"working (proxy={proxy_id}, threadId={thread_id[-8:]}) ..."),
            timeout_s=15.0,
            what="placeholder",
        )
        logger.info(f"op=tg.send ok=true trace_id={trace_id} chat_id={chat_id} msg_id={placeholder.message_id} kind=placeholder proxy_id={proxy_id}")

        task_msg: JsonDict = {
            "type": "task_assign",
            "trace_id": trace_id,
            "task_id": task_id,
            "thread_key": sk,
            "thread_id": thread_id,
            "prompt": prompt,
        }
        # Per app-server spec: set per-turn overrides (e.g. model) on turn/start.
        # This ensures the app-server thread defaults are updated and avoids drift
        # between manager local session prefs and the actual Codex thread settings.
        session_model = self._get_default_model(sk)
        if session_model:
            task_msg["model"] = session_model

        # Event-driven: send immediately, do not await result here.
        if chat_id is None:
            return
        await core.register_task(
            TaskContext(
                task_id=task_id,
                trace_id=trace_id,
                proxy_id=proxy_id,
                thread_id=thread_id,
                chat_id=int(chat_id),
                placeholder_msg_id=int(placeholder.message_id),
                created_at=time.time(),
                session_key=sk,
                result_mode=self._get_result_mode(sk),
            )
        )
        logger.info(
            f"op=dispatch.enqueue trace_id={trace_id} proxy_id={proxy_id} task_id={task_id} thread_id={thread_id[-8:]} "
            f"chat_id={chat_id} msg_id={placeholder.message_id} prompt_len={len(prompt)}"
        )
        asyncio.create_task(core.send_task_assign(proxy_id=proxy_id, task_msg=task_msg), name=f"send:{proxy_id}:{task_id}")


def main() -> int:
    cfg = _load_config()

    ap = argparse.ArgumentParser()
    ap.add_argument("--ws-listen", default=os.environ.get("CODEX_MANAGER_WS_LISTEN") or str(cfg.get("manager_ws_listen") or "0.0.0.0:8765"))
    ap.add_argument("--ws-only", action="store_true", help="run WS server only (no Telegram)")
    ap.add_argument("--dispatch-proxy", default="", help="WS-only helper: wait for proxy online then dispatch a prompt once and exit")
    ap.add_argument("--prompt", default="", help="WS-only helper prompt")
    ap.add_argument("--timeout", type=float, default=float(os.environ.get("CODEX_TASK_TIMEOUT", cfg.get("task_timeout_s") or 120.0)))
    ap.add_argument("--control-listen", default=os.environ.get("CODEX_MANAGER_CONTROL_LISTEN") or "", help="optional local control server listen (e.g. 127.0.0.1:18766)")
    ap.add_argument("--control-token", default=os.environ.get("CODEX_MANAGER_CONTROL_TOKEN") or "", help="required token for control server")
    args = ap.parse_args()

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or str(cfg.get("telegram_bot_token") or cfg.get("bot_token") or "")
    allowed_env = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    allowed_cfg = cfg.get("telegram_allowed_user_ids") if isinstance(cfg.get("telegram_allowed_user_ids"), list) else []
    allowed_users = _parse_allowed_user_ids(allowed_env)
    for x in allowed_cfg:
        try:
            allowed_users.add(int(x))
        except Exception:
            continue

    default_proxy = os.environ.get("CODEX_DEFAULT_PROXY") or str(cfg.get("default_proxy") or "")

    startup_notify_env = os.environ.get("TELEGRAM_STARTUP_NOTIFY_CHAT_IDS", "").strip()
    startup_notify_cfg = cfg.get("startup_notify_chat_ids") if isinstance(cfg.get("startup_notify_chat_ids"), list) else []
    startup_notify_chat_ids: list[int] = []
    if startup_notify_env:
        startup_notify_chat_ids = _parse_int_list_env(startup_notify_env)
    else:
        for x in startup_notify_cfg:
            try:
                startup_notify_chat_ids.append(int(x))
            except Exception:
                continue
        # Reasonable default: if caller already configured allowed user ids (private chat),
        # use them as startup notification chat ids.
        if not startup_notify_chat_ids and allowed_users:
            startup_notify_chat_ids = sorted(allowed_users)

    git_sha = _read_git_short_sha(BASE_DIR)

    proxies_cfg = cfg.get("proxies") if isinstance(cfg.get("proxies"), dict) else {}
    allowed_proxies: dict[str, str] = {}
    for pid, entry in proxies_cfg.items():
        if not isinstance(pid, str) or not pid.strip():
            continue
        token = ""
        if isinstance(entry, dict):
            token = str(entry.get("token") or "")
        elif isinstance(entry, str):
            token = entry
        if token:
            allowed_proxies[pid.strip()] = token

    if not allowed_proxies:
        logger.warning("No proxies allowlist configured; accepting ANY proxy registration (dev mode).")

    registry = ProxyRegistry(allowed=allowed_proxies)
    sessions = load_sessions()

    async def runner():
        core = ManagerCore(registry, task_timeout_s=args.timeout)
        ws_task = asyncio.create_task(run_ws_server(args.ws_listen, registry, core), name="ws_server")
        ctl_task: asyncio.Task | None = None
        if args.control_listen:
            if not args.control_token:
                raise SystemExit("control server enabled but missing --control-token (or CODEX_MANAGER_CONTROL_TOKEN)")
            ctl_task = asyncio.create_task(run_control_server(args.control_listen, args.control_token, registry, core), name="control_server")
        stop_event = asyncio.Event()

        def _stop() -> None:
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                pass

        if args.dispatch_proxy:
            # WS-only probe mode: wait for proxy then dispatch once and exit.
            deadline = time.time() + max(5.0, args.timeout)
            while time.time() < deadline:
                if registry.is_online(args.dispatch_proxy):
                    break
                await asyncio.sleep(0.2)
            if not registry.is_online(args.dispatch_proxy):
                raise SystemExit(f"proxy not online: {args.dispatch_proxy}")
            res = await core.dispatch_once(args.dispatch_proxy, args.prompt or "ping", timeout_s=args.timeout)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            stop_event.set()

        if args.ws_only or not bot_token:
            if not bot_token and not args.ws_only:
                logger.warning("Missing TELEGRAM_BOT_TOKEN; running WS-only.")
            await stop_event.wait()
            ws_task.cancel()
            await asyncio.gather(ws_task, return_exceptions=True)
            if ctl_task is not None:
                ctl_task.cancel()
                await asyncio.gather(ctl_task, return_exceptions=True)
            return

        app = ManagerApp(
            core=core,
            registry=registry,
            sessions=sessions,
            allowed_users=allowed_users,
            default_proxy=default_proxy,
            task_timeout_s=args.timeout,
        )
        async def telegram_loop() -> None:
            # Keep WS registry alive even if Telegram is temporarily unreachable.
            backoff_s = 2.0
            while not stop_event.is_set():
                tg = None
                try:
                    # Telegram 代理策略：
                    # - 优先使用显式配置：TELEGRAM_PROXY 或 manager_config.json 的 telegram_proxy
                    # - 若未显式配置，则继承系统 HTTP(S)_PROXY（trust_env=true）
                    proxy = (os.environ.get("TELEGRAM_PROXY") or str(cfg.get("telegram_proxy") or cfg.get("telegram_http_proxy") or "")).strip() or None
                    trust_env = proxy is None
                    req = HTTPXRequest(
                        connect_timeout=20.0,
                        read_timeout=60.0,
                        write_timeout=60.0,
                        pool_timeout=20.0,
                        proxy=proxy,
                        httpx_kwargs={"trust_env": trust_env},
                    )
                    tg = Application.builder().token(bot_token).request(req).build()
                    tg.bot_data["core"] = core
                    tg.add_handler(CommandHandler("help", app.cmd_help))
                    tg.add_handler(CommandHandler("start", app.cmd_start))
                    tg.add_handler(CommandHandler("ping", app.cmd_ping))
                    tg.add_handler(CommandHandler("approve", app.cmd_approve))
                    tg.add_handler(CommandHandler("approve_session", app.cmd_approve_session))
                    tg.add_handler(CommandHandler("decline", app.cmd_decline))
                    # Back-compat aliases.
                    tg.add_handler(CommandHandler("servers", app.cmd_proxy_list))
                    tg.add_handler(CommandHandler("use", app.cmd_proxy_use))
                    tg.add_handler(CommandHandler("proxy_list", app.cmd_proxy_list))
                    tg.add_handler(CommandHandler("proxy_use", app.cmd_proxy_use))
                    tg.add_handler(CommandHandler("proxy_current", app.cmd_proxy_current))
                    tg.add_handler(CommandHandler("status", app.cmd_status))
                    tg.add_handler(CommandHandler("model", app.cmd_model))
                    tg.add_handler(CallbackQueryHandler(app.on_model_callback, pattern=r"^model:"))
                    tg.add_handler(CommandHandler("result_mode", app.cmd_result_mode))
                    tg.add_handler(CommandHandler("thread_current", app.cmd_thread_current))
                    tg.add_handler(CommandHandler("thread_start", app.cmd_thread_start))
                    tg.add_handler(CommandHandler("thread_resume", app.cmd_thread_resume))
                    tg.add_handler(CommandHandler("thread_list", app.cmd_thread_list))
                    tg.add_handler(CommandHandler("thread_read", app.cmd_thread_read))
                    tg.add_handler(CommandHandler("thread_archive", app.cmd_thread_archive))
                    tg.add_handler(CommandHandler("thread_unarchive", app.cmd_thread_unarchive))
                    tg.add_handler(CommandHandler("skills_list", app.cmd_skills_list))
                    tg.add_handler(CommandHandler("config_read", app.cmd_config_read))
                    tg.add_handler(CommandHandler("config_value_write", app.cmd_config_value_write))
                    tg.add_handler(CommandHandler("collaborationmode_list", app.cmd_collaborationmode_list))
                    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, app.on_text))
                    tg.add_error_handler(lambda _u, c: logger.warning(f"telegram handler error: {c.error!r}"), block=False)

                    await tg.initialize()
                    # Ensure we're in polling mode. If a webhook is set, getUpdates won't deliver anything.
                    try:
                        await tg.bot.delete_webhook(drop_pending_updates=False)
                    except Exception as e:
                        logger.warning(f"delete_webhook failed: {type(e).__name__}: {e}")
                    await tg.start()
                    core.set_outbox(TelegramOutbox(tg.bot))
                    assert tg.updater is not None
                    await tg.updater.start_polling()
                    logger.info("Telegram polling started")
                    if startup_notify_chat_ids:
                        online = registry.online_proxy_ids()
                        text_lines = []
                        text_lines.append("codex_manager 已启动")
                        text_lines.append(f"time: {datetime.now().isoformat(timespec='seconds')}")
                        text_lines.append(f"host: {os.uname().nodename}")
                        if git_sha:
                            text_lines.append(f"git: {git_sha}")
                        text_lines.append(f"ws_listen: {args.ws_listen}")
                        if args.control_listen:
                            text_lines.append(f"control_listen: {args.control_listen}")
                        text_lines.append(f"proxies_online: {', '.join(online) if online else '(none)'}")
                        text_lines.append("tips: /help, /proxy_list, /proxy_use <id>, /status, /model")
                        msg_text = "\n".join(text_lines)
                        for chat_id in startup_notify_chat_ids:
                            try:
                                await _tg_call(tg.bot.send_message(chat_id=chat_id, text=msg_text), timeout_s=15.0, what="startup_notify")
                                logger.info(f"op=startup_notify ok=true chat_id={chat_id}")
                            except Exception as e:
                                logger.warning(f"op=startup_notify ok=false chat_id={chat_id} error={type(e).__name__}: {e}")
                    backoff_s = 2.0
                    await stop_event.wait()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Telegram start failed: {type(e).__name__}: {e} (retry in {backoff_s:.1f}s)")
                    await asyncio.sleep(backoff_s)
                    backoff_s = min(30.0, backoff_s * 1.7)
                finally:
                    if tg is not None:
                        try:
                            if tg.updater is not None:
                                await tg.updater.stop()
                        except Exception:
                            pass
                        try:
                            await tg.stop()
                        except Exception:
                            pass
                        try:
                            await tg.shutdown()
                        except Exception:
                            pass

        tg_task = asyncio.create_task(telegram_loop(), name="telegram_loop")
        try:
            await stop_event.wait()
        finally:
            tg_task.cancel()
            ws_task.cancel()
            if ctl_task is not None:
                ctl_task.cancel()
                await asyncio.gather(tg_task, ws_task, ctl_task, return_exceptions=True)
            else:
                await asyncio.gather(tg_task, ws_task, return_exceptions=True)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
