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
from pathlib import Path
from typing import Any

import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.request import HTTPXRequest

import log_rotate


JsonDict = dict[str, Any]

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "log"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "manager.log"
SESSIONS_FILE = BASE_DIR / "sessions.json"
CONFIG_FILE = BASE_DIR / "codex_config.json"
LEGACY_CONFIG_FILE = BASE_DIR / "opencode_config.json"


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
    cfg_path = CONFIG_FILE if CONFIG_FILE.exists() else LEGACY_CONFIG_FILE
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load config file {cfg_path}: {e}")
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
    if not SESSIONS_FILE.exists():
        return {}
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # Migration: old format stored Codex thread ids in "id" or was {user_id: session_id}.
        upgraded: dict[str, dict] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                upgraded[k] = {
                    "proxy": str(v.get("proxy") or v.get("server") or ""),
                    "pc_mode": bool(v.get("pc_mode", False)),
                    "reset_next": bool(v.get("reset_next", False)),
                }
            else:
                upgraded[k] = {"proxy": "", "pc_mode": False, "reset_next": False}
        return upgraded
    except Exception as e:
        logger.warning(f"Failed to load sessions: {e}")
        return {}


def save_sessions(sessions: dict[str, dict]) -> None:
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
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
    chat_id: int
    placeholder_msg_id: int
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
        self._thread_waiters: dict[str, asyncio.Future[JsonDict]] = {}

    def set_outbox(self, outbox: TelegramOutbox) -> None:
        self._outbox = outbox
        if self._timeout_task is None:
            self._timeout_task = asyncio.create_task(self._timeout_loop(), name="task_timeout_loop")

    async def register_task(self, ctx: TaskContext) -> None:
        async with self._lock:
            self._tasks_inflight[ctx.task_id] = ctx
            self._gc_recent_locked()

    async def dispatch_once(self, proxy_id: str, prompt: str, *, timeout_s: float) -> JsonDict:
        task_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        fut: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._waiters[task_id] = fut
        msg: JsonDict = {
            "type": "task_assign",
            "trace_id": trace_id,
            "task_id": task_id,
            "thread_key": "probe:local",
            "prompt": prompt,
            "reset_thread": True,
        }
        t0 = time.time()
        logger.info(f"op=dispatch.enqueue proxy_id={proxy_id} trace_id={trace_id} task_id={task_id} prompt_len={len(prompt)}")
        await self.registry.send_json(proxy_id, msg)
        try:
            res = await asyncio.wait_for(fut, timeout=timeout_s)
            latency_ms = int((time.time() - t0) * 1000.0)
            logger.info(f"op=dispatch.done proxy_id={proxy_id} trace_id={trace_id} task_id={task_id} ok={bool(res.get('ok'))} latency_ms={latency_ms}")
            return res
        finally:
            async with self._lock:
                self._waiters.pop(task_id, None)

    async def thread_op(self, proxy_id: str, thread_key: str, op: str, *, index: int | None = None, timeout_s: float = 30.0) -> JsonDict:
        """
        向某个 proxy 发送 thread 操作（会话栈），并等待 thread_op_result。

        协议：
          manager -> proxy: {"type":"thread_op","req_id":...,"trace_id":...,"thread_key":...,"op":"new|back|list|del","index":...}
          proxy -> manager: {"type":"thread_op_result","req_id":...,"trace_id":...,"ok":...,"sessions":[...],"depth":...,"error":...}
        """
        req_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        fut: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._thread_waiters[req_id] = fut
        msg: JsonDict = {
            "type": "thread_op",
            "req_id": req_id,
            "trace_id": trace_id,
            "thread_key": thread_key,
            "op": op,
        }
        if index is not None:
            msg["index"] = int(index)
        t0 = time.time()
        logger.info(f"op=thread_op.send proxy_id={proxy_id} trace_id={trace_id} req_id={req_id} op={op} thread_key={thread_key}")
        try:
            await self.registry.send_json(proxy_id, msg)
            res = await asyncio.wait_for(fut, timeout=timeout_s)
            latency_ms = int((time.time() - t0) * 1000.0)
            logger.info(
                f"op=thread_op.done proxy_id={proxy_id} trace_id={trace_id} req_id={req_id} "
                f"ok={bool(res.get('ok'))} latency_ms={latency_ms}"
            )
            return res
        finally:
            async with self._lock:
                self._thread_waiters.pop(req_id, None)

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
        if t == "task_ack":
            task_id = str(msg.get("task_id") or "")
            trace_id = str(msg.get("trace_id") or "")
            logger.info(f"op=ws.recv proxy_id={proxy_id} type=task_ack trace_id={trace_id} task_id={task_id}")
            return
        if t == "thread_op_result":
            req_id = str(msg.get("req_id") or "")
            trace_id = str(msg.get("trace_id") or "")
            logger.info(f"op=ws.recv proxy_id={proxy_id} type=thread_op_result trace_id={trace_id} req_id={req_id} ok={bool(msg.get('ok'))}")
            if not req_id:
                return
            async with self._lock:
                waiter = self._thread_waiters.get(req_id)
                if waiter and not waiter.done():
                    waiter.set_result(msg)
                    return
            logger.info(f"orphan thread_op_result: proxy={proxy_id} req_id={req_id} ok={bool(msg.get('ok'))}")
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
                if t in ("task_ack", "task_result", "thread_op_result"):
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
      {"type":"thread_op","token":"...","proxy_id":"proxy27","thread_key":"tg:1:2","op":"list|new|back|del","index":1,"timeout":30}

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
                    if not proxy_id:
                        writer.write((json.dumps({"ok": False, "type": "dispatch", "error": "missing proxy_id"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    try:
                        res = await core.dispatch_once(proxy_id, prompt, timeout_s=timeout_s)
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
                if t == "thread_op":
                    proxy_id = str(req.get("proxy_id") or "").strip()
                    thread_key = str(req.get("thread_key") or "").strip()
                    op = str(req.get("op") or "").strip()
                    index = req.get("index")
                    timeout_s = float(req.get("timeout") or 30.0)
                    if not proxy_id:
                        writer.write((json.dumps({"ok": False, "type": "thread_op", "error": "missing proxy_id"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    if not thread_key:
                        writer.write((json.dumps({"ok": False, "type": "thread_op", "error": "missing thread_key"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    if op not in ("list", "new", "back", "del"):
                        writer.write((json.dumps({"ok": False, "type": "thread_op", "error": f"bad op: {op}"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    try:
                        res = await core.thread_op(proxy_id, thread_key, op, index=(int(index) if index is not None else None), timeout_s=timeout_s)
                        resp = {"ok": True, "type": "thread_op", "result": res}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=thread_op ok=true proxy_id={proxy_id} op={op}")
                    except Exception as e:
                        resp = {"ok": False, "type": "thread_op", "error": f"{type(e).__name__}: {e}"}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=thread_op ok=false proxy_id={proxy_id} op={op} error={type(e).__name__}")
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
        registry: ProxyRegistry,
        sessions: dict[str, dict],
        allowed_users: set[int],
        default_proxy: str,
        task_timeout_s: float,
    ) -> None:
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

    async def cmd_servers(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /servers from chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/servers reply")
            return
        sk = _session_key(update)
        sess = self.sessions.get(sk) or {"proxy": "", "pc_mode": False, "reset_next": False}
        selected = str(sess.get("proxy") or "")
        online = self.registry.online_proxy_ids()
        allowed = self.registry.allowed_proxy_ids()
        lines = []
        lines.append(f"online: {', '.join(online) if online else '(none)'}")
        lines.append(f"selected: {selected or '(auto)'}")
        if allowed:
            lines.append(f"allowed: {', '.join(allowed)}")
        await _tg_call(update.message.reply_text("\n".join(lines)), timeout_s=15.0, what="/servers reply")

    async def cmd_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(
            f"cmd /ping from chat={update.effective_chat.id if update.effective_chat else '?'} "
            f"user={update.effective_user.id if update.effective_user else '?'}"
        )
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/ping reply")
            return
        await _tg_call(update.message.reply_text("pong"), timeout_s=15.0, what="/ping reply")

    async def cmd_use(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /use from chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/use reply")
            return
        sk = _session_key(update)
        if not context.args:
            await _tg_call(update.message.reply_text("usage: /use <proxy_id>"), timeout_s=15.0, what="/use reply")
            return
        proxy_id = str(context.args[0]).strip()
        if not proxy_id:
            await _tg_call(update.message.reply_text("usage: /use <proxy_id>"), timeout_s=15.0, what="/use reply")
            return
        if not self.registry.is_online(proxy_id):
            await _tg_call(update.message.reply_text(f"proxy offline: {proxy_id}"), timeout_s=15.0, what="/use reply")
            return
        sess = self.sessions.get(sk) or {"proxy": "", "pc_mode": False, "reset_next": False}
        sess["proxy"] = proxy_id
        sess["reset_next"] = True  # switch machine => reset remote thread mapping
        self.sessions[sk] = sess
        save_sessions(self.sessions)
        await _tg_call(update.message.reply_text(f"using proxy: {proxy_id} (next turn will reset)"), timeout_s=15.0, what="/use reply")

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /reset from chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/reset reply")
            return
        sk = _session_key(update)
        sess = self.sessions.get(sk) or {"proxy": "", "pc_mode": False, "reset_next": False}
        sess["reset_next"] = True
        self.sessions[sk] = sess
        save_sessions(self.sessions)
        await _tg_call(update.message.reply_text("ok (next turn will reset)"), timeout_s=15.0, what="/reset reply")

    async def cmd_pc(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /pc from chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what="/pc reply")
            return
        sk = _session_key(update)
        if not context.args or context.args[0] not in ("on", "off"):
            await _tg_call(update.message.reply_text("usage: /pc on|off"), timeout_s=15.0, what="/pc reply")
            return
        mode = context.args[0] == "on"
        sess = self.sessions.get(sk) or {"proxy": "", "pc_mode": False, "reset_next": False}
        sess["pc_mode"] = mode
        self.sessions[sk] = sess
        save_sessions(self.sessions)
        await _tg_call(update.message.reply_text(f"pc_mode={mode} (currently not used by protocol)"), timeout_s=15.0, what="/pc reply")

    def _require_selected_proxy(self, update: Update) -> str | None:
        sk = _session_key(update)
        sess = self.sessions.get(sk) or {"proxy": "", "pc_mode": False, "reset_next": False}
        selected_proxy = str(sess.get("proxy") or "").strip()
        return selected_proxy or None

    async def _thread_op_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE, op: str, *, index: int | None = None) -> None:
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(update.message.reply_text("unauthorized"), timeout_s=15.0, what=f"/{op} reply")
            return
        sk = _session_key(update)
        proxy_id = self._require_selected_proxy(update)
        if not proxy_id:
            await _tg_call(update.message.reply_text("请先 /servers 查看在线代理，然后 /use <proxy_id> 选择一台机器"), timeout_s=15.0, what=f"/{op} reply")
            return
        if not self.registry.is_online(proxy_id):
            await _tg_call(update.message.reply_text(f"proxy offline: {proxy_id} (use /servers)"), timeout_s=15.0, what=f"/{op} reply")
            return
        core: ManagerCore | None = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what=f"/{op} reply")
            return
        try:
            res = await core.thread_op(proxy_id, sk, op, index=index, timeout_s=min(30.0, self.task_timeout_s))
        except Exception as e:
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {type(e).__name__}: {e}"), timeout_s=15.0, what=f"/{op} reply")
            return
        if not bool(res.get("ok")):
            await _tg_call(update.message.reply_text(f"[{proxy_id}] error: {res.get('error') or 'unknown error'}"), timeout_s=15.0, what=f"/{op} reply")
            return
        sessions = res.get("sessions") if isinstance(res.get("sessions"), list) else []
        depth = int(res.get("depth") or 0)
        lines: list[str] = []
        lines.append(f"proxy: {proxy_id}")
        lines.append(f"depth: {depth}")
        if sessions:
            for i, sid in enumerate(sessions, start=1):
                mark = " (current)" if i == len(sessions) else ""
                lines.append(f"{i}. {sid}{mark}")
        else:
            lines.append("(empty)")
        lines.append("commands: /new /back /sessions /sessiondel <idx>")
        await _tg_call(update.message.reply_text("\n".join(lines)), timeout_s=15.0, what=f"/{op} reply")

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /new from chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        await self._thread_op_cmd(update, context, "new")

    async def cmd_back(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /back from chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        await self._thread_op_cmd(update, context, "back")

    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(f"cmd /sessions from chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        await self._thread_op_cmd(update, context, "list")

    async def cmd_sessiondel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info(
            f"cmd /sessiondel from chat={update.effective_chat.id if update.effective_chat else '?'} "
            f"user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}"
        )
        if not update.message:
            return
        if not context.args:
            await _tg_call(update.message.reply_text("usage: /sessiondel <idx>"), timeout_s=15.0, what="/sessiondel reply")
            return
        try:
            idx = int(str(context.args[0]).strip())
        except Exception:
            await _tg_call(update.message.reply_text("usage: /sessiondel <idx>"), timeout_s=15.0, what="/sessiondel reply")
            return
        await self._thread_op_cmd(update, context, "del", index=idx)

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
        sess = self.sessions.get(sk) or {"proxy": "", "pc_mode": False, "reset_next": False}
        selected_proxy = str(sess.get("proxy") or "").strip()
        if not selected_proxy:
            await _tg_call(update.message.reply_text("请先 /servers 查看在线代理，然后 /use <proxy_id> 选择一台机器"), timeout_s=15.0, what="msg reply")
            return
        proxy_id = selected_proxy
        if not self.registry.is_online(proxy_id):
            await _tg_call(update.message.reply_text(f"proxy offline: {proxy_id} (use /servers)"), timeout_s=15.0, what="msg reply")
            return

        task_id = uuid.uuid4().hex
        reset_thread = bool(sess.get("reset_next", False))
        sess["reset_next"] = False
        self.sessions[sk] = sess
        save_sessions(self.sessions)

        prompt = update.message.text
        placeholder = await _tg_call(
            update.message.reply_text(f"working (proxy={proxy_id}, reset={reset_thread}) ..."),
            timeout_s=15.0,
            what="placeholder",
        )
        logger.info(f"op=tg.send ok=true trace_id={trace_id} chat_id={chat_id} msg_id={placeholder.message_id} kind=placeholder proxy_id={proxy_id}")

        task_msg: JsonDict = {
            "type": "task_assign",
            "trace_id": trace_id,
            "task_id": task_id,
            "thread_key": sk,
            "prompt": prompt,
            "reset_thread": reset_thread,
        }

        # Event-driven: send immediately, do not await result here.
        core = context.application.bot_data.get("core")
        if core is None:
            await _tg_call(placeholder.edit_text(f"[{proxy_id}] error: manager core missing"), timeout_s=15.0, what="edit error")
            return
        if chat_id is None:
            return
        await core.register_task(
            TaskContext(
                task_id=task_id,
                trace_id=trace_id,
                proxy_id=proxy_id,
                chat_id=int(chat_id),
                placeholder_msg_id=int(placeholder.message_id),
                created_at=time.time(),
            )
        )
        logger.info(f"op=dispatch.enqueue trace_id={trace_id} proxy_id={proxy_id} task_id={task_id} chat_id={chat_id} msg_id={placeholder.message_id} prompt_len={len(prompt)}")
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
                    # 只使用显式配置的 Telegram 代理，避免被系统 HTTP(S)_PROXY 环境变量“误伤”。
                    # 如需代理：在 codex_config.json 里配置 telegram_proxy，或设置 TELEGRAM_PROXY。
                    proxy = (os.environ.get("TELEGRAM_PROXY") or str(cfg.get("telegram_proxy") or cfg.get("telegram_http_proxy") or "")).strip() or None
                    req = HTTPXRequest(
                        connect_timeout=20.0,
                        read_timeout=60.0,
                        write_timeout=60.0,
                        pool_timeout=20.0,
                        proxy=proxy,
                        httpx_kwargs={"trust_env": False},
                    )
                    tg = Application.builder().token(bot_token).request(req).build()
                    tg.bot_data["core"] = core
                    tg.add_handler(CommandHandler("servers", app.cmd_servers))
                    tg.add_handler(CommandHandler("ping", app.cmd_ping))
                    tg.add_handler(CommandHandler("use", app.cmd_use))
                    tg.add_handler(CommandHandler("reset", app.cmd_reset))
                    tg.add_handler(CommandHandler("pc", app.cmd_pc))
                    tg.add_handler(CommandHandler("new", app.cmd_new))
                    tg.add_handler(CommandHandler("back", app.cmd_back))
                    tg.add_handler(CommandHandler("sessions", app.cmd_sessions))
                    tg.add_handler(CommandHandler("sessiondel", app.cmd_sessiondel))
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
