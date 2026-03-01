import argparse
import asyncio
import faulthandler
import json
import logging
import os
import re
import socket
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.request import HTTPXRequest

import log_rotate
from manager.auth.node_auth_service import NodeAuthService
from manager.auth.tg_user_auth_service import TgUserAuthService
from manager.infra.migrations import migrate_legacy_sessions_if_needed
from manager.infra.node_registry import NodeRegistry
from manager.infra.repo_sessions import SessionStore
from manager.infra.ws_node_server import run_ws_server
from manager.infra.ws_control_server import run_control_server
from manager.service.manager_core import ManagerCore
from manager.service.session_service import SessionService
from manager.service.task_models import TaskContext
from bot_comm.handlers.model_handlers import ModelHandlers
from bot_comm.handlers.misc_command_handlers import MiscCommandHandlers
from bot_comm.handlers.node_handlers import NodeHandlers
from bot_comm.handlers.approval_handlers import ApprovalHandlers
from bot_comm.handlers.general_handlers import GeneralHandlers
from bot_comm.handlers.remote_command_handlers import RemoteCommandHandlers
from bot_comm.handlers.status_handlers import StatusHandlers
from bot_comm.handlers.thread_handlers import ThreadHandlers
from bot_comm.handlers.thread_methods_handlers import ThreadMethodsHandlers
from bot_comm.handlers.text_message_handler import TextMessageHandler
from bot_comm.handlers.token_handlers import TokenHandlers
from bot_comm.infra.telegram_outbox import TelegramOutbox


JsonDict = dict[str, Any]

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOG_DIR = BASE_DIR / "log"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "manager.log"
CONFIG_FILE = BASE_DIR / "manager_config.json"
DB_FILE = BASE_DIR / "manager_data.db"
LEGACY_SESSIONS_FILE = BASE_DIR / "sessions.json"


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

async def _tg_call(make_coro, *, timeout_s: float, what: str, retries: int = 3) -> Any:
    """
    Telegram 网络在部分环境下不稳定（尤其是走本地代理时），这里做轻量重试避免“没反应”。

    注意：send_message 不是严格幂等，重试可能导致重复消息；但比起完全无响应更可接受。

    约定：如果调用方本身已经实现了“队列级重试”（例如 TelegramOutbox），则应把 retries 设为 1，
    避免“单次发送内部重试 + 队列外部重试”叠加导致整体延迟过大。
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            # Must create a new coroutine per attempt; coroutines can't be awaited twice.
            return await asyncio.wait_for(make_coro(), timeout=timeout_s)
        except BadRequest as e:
            # PTB treats BadRequest as a NetworkError subclass.
            # For edits, Telegram returns "Message is not modified" when the content is identical.
            # This shouldn't be retried or treated as a real failure.
            msg = str(e)
            if "Message is not modified" in msg:
                logger.info(f"telegram call noop ({what}): {type(e).__name__}: {msg}")
                return None
            logger.warning(f"telegram call bad request ({what}): {type(e).__name__}: {msg}")
            raise
        except RetryAfter as e:
            # Telegram 限流：按建议等待后重试。
            wait_s = max(0.5, float(getattr(e, "retry_after", 1.0)))
            if attempt >= retries:
                logger.warning(f"telegram call rate-limited ({what}): RetryAfter {wait_s:.1f}s (attempt {attempt}/{retries})")
                raise
            logger.info(f"telegram call rate-limited ({what}): RetryAfter {wait_s:.1f}s (attempt {attempt}/{retries})")
            await asyncio.sleep(wait_s)
        except (TimedOut, NetworkError, asyncio.TimeoutError) as e:
            if attempt >= retries:
                logger.warning(f"telegram call transient failed ({what}): {type(e).__name__}: {e} (attempt {attempt}/{retries})")
                raise
            logger.info(f"telegram call transient failed ({what}): {type(e).__name__}: {e} (attempt {attempt}/{retries})")
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


def _parse_csv_set(value: str) -> set[str]:
    out: set[str] = set()
    for part in (value or "").replace(" ", ",").split(","):
        p = part.strip()
        if p:
            out.add(p)
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
    Split text into Telegram-safe chunks and add a node prefix to each chunk.
    Telegram hard limit is 4096; we keep a buffer via default limit.
    """
    prefix = prefix or ""
    if not prefix:
        return _split_telegram_text(text, limit=limit)
    # Keep at least 200 chars for payload even if prefix is long.
    per_part_limit = max(200, limit - len(prefix))
    parts = _split_telegram_text(text, limit=per_part_limit)
    return [prefix + p for p in parts]


def _short_one_line(s: str, limit: int = 80) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 3)] + "..."


def _result_fingerprint(text: str, *, parts: int) -> str:
    body = text or ""
    head = _short_one_line(body[:200], limit=60)
    tail = _short_one_line(body[-200:], limit=60) if len(body) > 0 else ""
    ln = len(body)
    return f'result: len={ln} parts={parts} head=\"{head}\" tail=\"{tail}\"'


def _detect_public_ipv4_no_proxy(timeout_s: int = 4) -> str:
    # Prefer domestic endpoints; bypass proxy explicitly.
    endpoints = [
        "https://4.ipw.cn",
        "https://ip.3322.net",
        "https://myip.ipip.net",
    ]
    ip_re = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})\b")
    for url in endpoints:
        try:
            cp = subprocess.run(
                ["curl", "--noproxy", "*", "-4", "-fsS", "--max-time", str(int(timeout_s)), url],
                capture_output=True,
                text=True,
                check=True,
            )
            txt = (cp.stdout or "").strip()
            m = ip_re.search(txt)
            if m:
                ip = m.group(1).strip()
                parts = ip.split(".")
                if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                    return ip
        except Exception:
            continue
    return ""


def _resolve_manager_public_ws(*, ws_listen: str, cfg: dict[str, Any], logger: logging.Logger) -> str:
    public_ws = (os.environ.get("CODEX_MANAGER_PUBLIC_WS") or str(cfg.get("public_manager_ws") or "")).strip()
    if public_ws:
        return public_ws
    port = str(ws_listen).rsplit(":", 1)[-1]
    pub_ip = _detect_public_ipv4_no_proxy(timeout_s=4)
    if pub_ip:
        out = f"ws://{pub_ip}:{port}"
        logger.info(f"resolved manager public ws via curl --noproxy: {out}")
        return out
    guessed_ip = ""
    try:
        guessed_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        guessed_ip = ""
    out = f"ws://{guessed_ip or '<MANAGER_IP>'}:{port}"
    logger.warning(f"fallback manager public ws: {out}")
    return out


SESSION_STORE: SessionStore | None = None


def load_sessions() -> dict[str, dict]:
    if SESSION_STORE is None:
        return {}
    try:
        return SESSION_STORE.load_all()
    except Exception as e:
        logger.warning(f"Failed to load sessions from db: {e}")
        return {}


def save_sessions(sessions: dict[str, dict]) -> None:
    if SESSION_STORE is None:
        return
    try:
        SESSION_STORE.save_all(sessions)
    except Exception as e:
        logger.warning(f"Failed to save sessions to db: {e}")


def _session_key(update: Update) -> str:
    assert update.effective_chat is not None
    assert update.effective_user is not None
    return f"tg:{update.effective_chat.id}:{update.effective_user.id}"


class ManagerApp:
    def __init__(
        self,
        core: ManagerCore,
        registry: NodeRegistry,
        sessions: dict[str, dict],
        node_auth: NodeAuthService,
        tg_auth: TgUserAuthService,
        default_node: str,
        manager_public_ws: str,
        task_timeout_s: float,
        ws_listen: str = "",
        control_listen: str = "",
        git_sha: str = "",
        started_at_ts: float = 0.0,
    ) -> None:
        self.core = core
        self.logger = logger
        self.registry = registry
        self.sessions = sessions
        self.session_service = SessionService(self.sessions)
        self.node_auth = node_auth
        self.tg_auth = tg_auth
        self.default_node = default_node
        self.manager_public_ws = manager_public_ws
        self.task_timeout_s = task_timeout_s
        self.ws_listen = ws_listen
        self.control_listen = control_listen
        self.git_sha = git_sha
        self.started_at_ts = float(started_at_ts or time.time())
        self.node_handlers = NodeHandlers(
            registry=self.registry,
            is_allowed=self._is_allowed,
            session_key_fn=_session_key,
            get_selected_node=self._get_selected_node,
            set_selected_node=self._set_selected_node,
            save_sessions_fn=save_sessions,
            sessions_ref=self.sessions,
            tg_call=_tg_call,
            logger=logger,
        )
        self.model_handlers = ModelHandlers(
            registry=self.registry,
            is_allowed=self._is_allowed,
            session_key_fn=_session_key,
            get_selected_node=self._get_selected_node,
            get_default_model=self._get_default_model,
            set_default_model=self._set_default_model,
            get_default_effort=self._get_default_effort,
            set_default_effort=self._set_default_effort,
            save_sessions_fn=save_sessions,
            sessions_ref=self.sessions,
            require_node_online=self._require_node_online,
            tg_call=_tg_call,
            task_timeout_s=self.task_timeout_s,
            logger=logger,
        )
        self.status_handlers = StatusHandlers(
            base_dir=BASE_DIR,
            registry=self.registry,
            is_allowed=self._is_allowed,
            session_key_fn=_session_key,
            get_selected_node=self._get_selected_node,
            get_default_model=self._get_default_model,
            get_current_thread_id=self._get_current_thread_id,
            get_result_mode=self._get_result_mode,
            tg_call=_tg_call,
            task_timeout_s=self.task_timeout_s,
            logger=logger,
        )
        self.thread_handlers = ThreadHandlers(
            is_allowed=self._is_allowed,
            tg_call=_tg_call,
            cmd_thread_current=self.cmd_thread_current,
            cmd_thread_start=self.cmd_thread_start,
            cmd_thread_resume=self.cmd_thread_resume,
            cmd_thread_list=self.cmd_thread_list,
            cmd_thread_read=self.cmd_thread_read,
            cmd_thread_archive=self.cmd_thread_archive,
            cmd_thread_unarchive=self.cmd_thread_unarchive,
            logger=logger,
        )
        self.thread_methods_handlers = ThreadMethodsHandlers(
            is_allowed=self._is_allowed,
            tg_call=_tg_call,
            parse_kv=self._parse_kv,
            require_node_online=self._require_node_online,
            session_key_fn=_session_key,
            get_current_thread_id=self._get_current_thread_id,
            set_current_thread_id=self._set_current_thread_id,
            get_default_model=self._get_default_model,
            save_sessions_fn=save_sessions,
            sessions_ref=self.sessions,
            pretty_json=self._pretty_json,
            task_timeout_s=self.task_timeout_s,
            logger=logger,
        )
        self.misc_handlers = MiscCommandHandlers(
            tg_call=_tg_call,
            logger=logger,
            cmd_config_read=self.cmd_config_read,
            cmd_config_value_write=self.cmd_config_value_write,
            cmd_skills_list=self.cmd_skills_list,
            cmd_collaborationmode_list=self.cmd_collaborationmode_list,
        )
        self.remote_command_handlers = RemoteCommandHandlers(
            is_allowed=self._is_allowed,
            tg_call=_tg_call,
            parse_kv=self._parse_kv,
            require_node_online=self._require_node_online,
            pretty_json=self._pretty_json,
            task_timeout_s=self.task_timeout_s,
            logger=logger,
        )
        self.text_message_handler = TextMessageHandler(
            is_allowed=self._is_allowed,
            session_key_fn=_session_key,
            get_selected_node=self._get_selected_node,
            get_current_thread_id=self._get_current_thread_id,
            set_current_thread_id=self._set_current_thread_id,
            get_default_model=self._get_default_model,
            get_default_effort=self._get_default_effort,
            get_result_mode=self._get_result_mode,
            tg_call=_tg_call,
            registry_is_online=self.registry.is_online,
            save_sessions_fn=save_sessions,
            sessions_ref=self.sessions,
            task_context_cls=TaskContext,
            base_dir=BASE_DIR,
            task_timeout_s=self.task_timeout_s,
            logger=logger,
        )
        self.token_handlers = TokenHandlers(
            node_auth=self.node_auth,
            is_allowed=self._is_allowed,
            parse_kv=self._parse_kv,
            fmt_unix_ts=self._fmt_unix_ts,
            build_node_config=self._build_node_config,
            tg_call=_tg_call,
            logger=logger,
        )
        self.approval_handlers = ApprovalHandlers(
            is_allowed=self._is_allowed,
            tg_call=_tg_call,
            core=self.core,
            logger=logger,
        )
        self.general_handlers = GeneralHandlers(
            is_allowed=self._is_allowed,
            tg_call=_tg_call,
            session_key_fn=_session_key,
            get_result_mode=self._get_result_mode,
            set_result_mode=self._set_result_mode,
            save_sessions_fn=save_sessions,
            sessions_ref=self.sessions,
            logger=logger,
        )

    def _is_allowed(self, update: Update) -> bool:
        uid = update.effective_user.id if update.effective_user else None
        return self.tg_auth.is_allowed(uid if isinstance(uid, int) else None)

    def _choose_node(self, session_node: str) -> str | None:
        if session_node and self.registry.is_online(session_node):
            return session_node
        if self.default_node and self.registry.is_online(self.default_node):
            return self.default_node
        online = self.registry.online_node_ids()
        return online[0] if online else None

    def _get_sess(self, sk: str) -> dict:
        return self.session_service.get_sess(sk)

    def _get_selected_node(self, sk: str) -> str:
        return self.session_service.get_selected_node(sk)

    def _get_result_mode(self, sk: str) -> str:
        return self.session_service.get_result_mode(sk)

    def _set_result_mode(self, sk: str, mode: str) -> None:
        self.session_service.set_result_mode(sk, mode)

    def _set_selected_node(self, sk: str, node_id: str) -> None:
        self.session_service.set_selected_node(sk, node_id)

    def _get_current_thread_id(self, sk: str, node_id: str) -> str:
        return self.session_service.get_current_thread_id(sk, node_id)

    def _get_current_thread_id_for_node(self, sk: str, node_id: str) -> str:
        return self._get_current_thread_id(sk, node_id)

    def _get_defaults(self, sk: str) -> dict[str, Any]:
        return self.session_service.get_defaults(sk)

    def _get_default_model(self, sk: str) -> str:
        return self.session_service.get_default_model(sk)

    def _set_default_model(self, sk: str, model: str) -> None:
        self.session_service.set_default_model(sk, model)

    def _get_default_effort(self, sk: str) -> str:
        return self.session_service.get_default_effort(sk)

    def _set_default_effort(self, sk: str, effort: str) -> None:
        self.session_service.set_default_effort(sk, effort)

    def _set_current_thread_id(self, sk: str, node_id: str, thread_id: str) -> None:
        self.session_service.set_current_thread_id(sk, node_id, thread_id)

    def _set_current_thread_id_for_node(self, sk: str, node_id: str, thread_id: str) -> None:
        self._set_current_thread_id(sk, node_id, thread_id)

    def _build_node_config(self, node_id: str, token: str) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "manager_ws": self.manager_public_ws,
            "node_id": node_id,
            "node_token": token,
            "max_pending": 10,
            "sandbox": "dangerFullAccess",
            "approval_policy": "onRequest",
            "codex_cwd": str(BASE_DIR),
            "codex_bin": "codex",
        }
        return cfg

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

    async def _fetch_model_state(self, *, node_id: str, core: ManagerCore, session_model: str) -> tuple[str, str, list[str]]:
        cfg_rep = await core.appserver_call(node_id, "config/read", {}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(cfg_rep.get("ok")):
            raise RuntimeError(str(cfg_rep.get("error") or "config/read failed"))
        cfg_result = cfg_rep.get("result") if isinstance(cfg_rep.get("result"), dict) else {}
        cfg_obj = cfg_result.get("config") if isinstance(cfg_result.get("config"), dict) else {}
        proxy_default_model = str(cfg_obj.get("model") or "").strip()
        effective_model = session_model or proxy_default_model

        rep = await core.appserver_call(node_id, "model/list", {}, timeout_s=min(60.0, self.task_timeout_s))
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

    async def _fetch_model_detail(self, *, node_id: str, core: ManagerCore, model_id: str) -> JsonDict:
        rep = await core.appserver_call(node_id, "model/list", {}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            raise RuntimeError(str(rep.get("error") or "model/list failed"))
        result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
        data = result.get("data") if isinstance(result.get("data"), list) else []
        for item in data:
            if isinstance(item, dict) and str(item.get("id") or "").strip() == model_id:
                return item
        return {}

    def _render_model_text(self, *, node_id: str, effective_model: str, proxy_default_model: str, session_model: str, models: list[str], effort: str, model_detail: JsonDict | None) -> str:
        lines = [f"model: {effective_model or '(unknown)'}", f"node: {node_id}"]
        lines.append(f"effort: {effort}")
        if session_model:
            lines.append("source: session override")
        elif proxy_default_model:
            lines.append("source: node default")
        if isinstance(model_detail, dict) and model_detail:
            de = str(model_detail.get("defaultReasoningEffort") or "").strip()
            if de:
                lines.append(f"defaultReasoningEffort: {de}")
            sup = model_detail.get("supportedReasoningEfforts")
            if isinstance(sup, list) and sup:
                lines.append(f"supportedReasoningEfforts: {', '.join([str(x) for x in sup if x])}")
        # Keep the text compact: available models are selectable via buttons.
        if models:
            shown = min(len(models), 12)
            lines.append(f"available: {shown} shown in buttons (total={len(models)})")
        else:
            lines.append("available: (none)")
        return "\n".join(lines)

    def _build_model_keyboard(self, *, models: list[str], effective_model: str, session_model: str, effort: str) -> InlineKeyboardMarkup:
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
        # Effort controls (low/medium/high). Default is medium.
        eff_row: list[InlineKeyboardButton] = []
        for e in ("low", "medium", "high"):
            lab = f"• {e}" if e == effort else e
            eff_row.append(InlineKeyboardButton(lab, callback_data=f"effort:set:{e}"))
        rows.append(eff_row)
        rows.append([
            InlineKeyboardButton("Clear", callback_data="model:clear"),
            InlineKeyboardButton("Refresh", callback_data="model:refresh"),
        ])
        return InlineKeyboardMarkup(rows)

    def _render_node_text(self, *, selected: str, online: list[str], allowed: list[str]) -> str:
        return self.node_handlers.render_node_text(selected=selected, online=online, allowed=allowed)

    def _build_node_keyboard(self, *, online: list[str], selected: str) -> InlineKeyboardMarkup:
        return self.node_handlers.build_node_keyboard(online=online, selected=selected)

    async def cmd_node(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.node_handlers.cmd_node(update, context)

    async def on_node_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.node_handlers.on_node_callback(update, context)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.status_handlers.cmd_status(update, context)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.model_handlers.cmd_model(update, context)

    async def on_model_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.model_handlers.on_model_callback(update, context)

    async def on_effort_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.model_handlers.on_effort_callback(update, context)

    async def cmd_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.general_handlers.cmd_ping(update, context)

    async def cmd_result(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.general_handlers.cmd_result(update, context)

    async def cmd_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.token_handlers.cmd_token(update, context)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /help chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/help reply")
            return

        lines: list[str] = []
        lines.append("Codex Manager (TG -> Manager -> Node -> Codex app-server)")
        lines.append("")
        lines.append("1) 选择机器（node）")
        lines.append("- /node  弹出在线机器按钮，并显示 current")
        lines.append("- /status  查看当前会话状态汇总")
        lines.append("- /manager 查看 manager 运行信息")
        lines.append("- /model  查看当前会话模型（以及 node 默认模型），并列出可点按钮切换")
        lines.append("- /model <model_id>  切换当前会话模型（会在每次 turn/start 里下发，按 app-server 语义写回 thread 默认）")
        lines.append("- /result [replace|send]  结果输出模式：覆盖占位 / 单独发结果")
        lines.append("")
        lines.append("2) 日常对话（turn）")
        lines.append("- 直接发送文本即可。")
        lines.append("- 若当前 node 没有 thread，会自动 thread/start。")
        lines.append(f"- 运行中会用 placeholder 追加进度日志，默认约每 {int(self.core.progress_update_interval_s) if hasattr(self, 'core') else 5} 秒最多更新一次。")
        lines.append("")
        lines.append("3) Thread 会话（对齐 app-server method）")
        lines.append("- /thread current")
        lines.append("- /thread start [cwd=...] [sandbox=workspaceWrite] [approvalPolicy=onRequest] [personality=pragmatic]")
        lines.append("- /thread resume <id>  (也支持: threadId=<id>)")
        lines.append("- /thread list [limit=5] [archived=true|false] [cursor=...] [sortKey=created_at|updated_at]")
        lines.append("- /thread read <id> [includeTurns=true|false]  (也支持: threadId=<id>)")
        lines.append("- /thread archive [threadId=<id>]   (不填则归档当前 thread)")
        lines.append("- /thread unarchive <id>  (也支持: threadId=<id>)")
        lines.append("")
        lines.append("4) 其它 app-server 查询/配置")
        lines.append("- /model")
        lines.append("- /model <model_id>")
        lines.append("- /token generate node_id=<node_id> [note=<text>]  生成 token + 完整 node_config.json")
        lines.append("- /token <node_id>  快捷生成 token + node_config.json")
        lines.append("- /token list [revoked=true|false]  查询 token")
        lines.append("- /token revoke <token_id>  废除 token")
        lines.append("- /skills list [cwds=/a,/b] [forceReload=true|false]")
        lines.append("- /config read [includeLayers=true|false]")
        lines.append("- /config write keyPath=<...> value=<json> [mergeStrategy=replace|upsert]")
        lines.append("- /collaborationmode list")
        lines.append("")
        lines.append("5) 审批（approval）")
        lines.append("- 当 approvalPolicy=onRequest 时，某些命令/文件变更会触发审批。")
        lines.append("- /approve <approval_id>  同意一次")
        lines.append("- /approve session <approval_id>  本会话同意（如果 codex 支持）")
        lines.append("- /decline <approval_id>  拒绝")
        lines.append("")
        lines.append("参数格式：key=value（多个参数用空格分隔）。JSON 参数用 value=<json>。")
        lines.append("示例：")
        lines.append("- /node")
        lines.append("- /model gpt-5-codex")
        lines.append("- /result send")
        lines.append("- /thread list limit=3 archived=false")
        lines.append("- /config write keyPath=apps._default.enabled value=true mergeStrategy=replace")
        lines.append("")
        lines.append("提示：thread 内容由 Codex 保存在 node 机器的 ~/.codex/；manager 只保存 chat->(node, threadId) 路由。")

        await _tg_call(lambda: update.message.reply_text("\n".join(lines)), timeout_s=15.0, what="/help reply")

    async def cmd_manager(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /manager chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not update.message:
            return
        if not self._is_allowed(update):
            await _tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/manager reply")
            return
        now = time.time()
        uptime_s = max(0, int(now - self.started_at_ts))
        h = uptime_s // 3600
        m = (uptime_s % 3600) // 60
        s = uptime_s % 60
        online = self.registry.online_node_ids()
        lines: list[str] = []
        lines.append("manager:")
        lines.append(f"uptime: {h:02d}:{m:02d}:{s:02d}")
        lines.append(f"git: {self.git_sha or '(unknown)'}")
        lines.append(f"ws_listen: {self.ws_listen or '(unknown)'}")
        lines.append(f"manager_ws(public): {self.manager_public_ws or '(unknown)'}")
        lines.append(f"control_listen: {self.control_listen or '(disabled)'}")
        lines.append(f"default_node: {self.default_node or '(none)'}")
        lines.append(f"online_count: {len(online)}")
        if online:
            lines.append(f"online_nodes: {', '.join(online)}")
        await _tg_call(lambda: update.message.reply_text("\n".join(lines)), timeout_s=15.0, what="/manager reply")

    async def cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.approval_handlers.cmd_approve(update, context)

    async def cmd_decline(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.approval_handlers.cmd_decline(update, context)

    async def on_approve_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.approval_handlers.on_approve_callback(update, context)

    async def cmd_thread(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.thread_handlers.cmd_thread(update, context)

    async def on_thread_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.thread_handlers.on_thread_callback(update, context)

    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.misc_handlers.cmd_config(update, context)

    async def cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.misc_handlers.cmd_skills(update, context)

    async def cmd_collaborationmode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.misc_handlers.cmd_collaborationmode(update, context)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Telegram common entrypoint.
        await self.cmd_help(update, context)

    async def _require_node_online(self, update: Update) -> str | None:
        msg = update.effective_message
        if msg is None:
            return None
        sk = _session_key(update)
        node_id = self._get_selected_node(sk)
        if not node_id:
            await _tg_call(lambda: msg.reply_text("请先 /node 选择一台 node"), timeout_s=15.0, what="require node")
            return None
        if not self.registry.is_online(node_id):
            await _tg_call(lambda: msg.reply_text(f"node offline: {node_id} (use /node)"), timeout_s=15.0, what="require node")
            return None
        return node_id

    async def _send_typing_hint(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        what: str,
    ) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        try:
            await _tg_call(
                lambda: context.bot.send_chat_action(chat_id=chat.id, action="typing"),
                timeout_s=5.0,
                what=what,
                retries=1,
            )
            self.logger.info(f"typing hint sent ({what}) chat={chat.id}")
        except Exception as e:
            self.logger.warning(f"typing hint failed ({what}): {type(e).__name__}: {e}")
            return

    def _with_typing(
        self,
        handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]],
        *,
        what: str,
    ) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]:
        async def _wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            # Send one immediate typing hint before handler work starts.
            await self._send_typing_hint(update, context, what=what)
            stop = asyncio.Event()

            async def _typing_loop() -> None:
                # Telegram typing status expires quickly; refresh periodically until handler returns.
                while not stop.is_set():
                    await self._send_typing_hint(update, context, what=what)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=4.0)
                    except asyncio.TimeoutError:
                        continue

            t = asyncio.create_task(_typing_loop(), name=f"tg_typing_wrap:{what}")
            try:
                await handler(update, context)
            finally:
                stop.set()
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)

        return _wrapped

    async def cmd_thread_current(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.thread_methods_handlers.cmd_thread_current(update, context)

    async def cmd_thread_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.thread_methods_handlers.cmd_thread_start(update, context)

    async def cmd_thread_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.thread_methods_handlers.cmd_thread_resume(update, context)

    async def cmd_thread_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.thread_methods_handlers.cmd_thread_list(update, context)

    async def cmd_thread_read(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.thread_methods_handlers.cmd_thread_read(update, context)

    async def cmd_thread_archive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.thread_methods_handlers.cmd_thread_archive(update, context)

    async def cmd_thread_unarchive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.thread_methods_handlers.cmd_thread_unarchive(update, context)

    async def cmd_skills_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.remote_command_handlers.cmd_skills_list(update, context)

    async def cmd_config_read(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.remote_command_handlers.cmd_config_read(update, context)

    async def cmd_config_value_write(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.remote_command_handlers.cmd_config_value_write(update, context)

    async def cmd_collaborationmode_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.remote_command_handlers.cmd_collaborationmode_list(update, context)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.text_message_handler.on_text(update, context)


def main() -> int:
    global SESSION_STORE
    cfg = _load_config()

    ap = argparse.ArgumentParser()
    ap.add_argument("--ws-listen", default=os.environ.get("CODEX_MANAGER_WS_LISTEN") or str(cfg.get("manager_ws_listen") or "0.0.0.0:8765"))
    ap.add_argument("--ws-only", action="store_true", help="run WS server only (no Telegram)")
    ap.add_argument("--dispatch-node", default="", help="WS-only helper: wait for node online then dispatch a prompt once and exit")
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
    tg_auth = TgUserAuthService(DB_FILE, seed_users=allowed_users)

    default_node = os.environ.get("CODEX_DEFAULT_NODE") or str(cfg.get("default_node") or "")

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

    node_notify_env = os.environ.get("TELEGRAM_NODE_NOTIFY_CHAT_IDS", "").strip()
    node_notify_min_interval_s = float(os.environ.get("TELEGRAM_NODE_NOTIFY_MIN_INTERVAL_S", "60") or "60")
    node_notify_chat_ids: list[int] = []
    if node_notify_env:
        node_notify_chat_ids = _parse_int_list_env(node_notify_env)
    else:
        # Default: follow startup notifications (usually same private chat ids).
        node_notify_chat_ids = list(startup_notify_chat_ids)

    git_sha = _read_git_short_sha(BASE_DIR)

    db_exists_before = DB_FILE.exists()
    node_auth = NodeAuthService(DB_FILE)
    logger.info(f"node token auth enabled; db={DB_FILE} active_tokens={node_auth.active_count()}")

    SESSION_STORE = SessionStore(DB_FILE)
    registry = NodeRegistry(node_auth=node_auth, logger=logger)
    migrate_legacy_sessions_if_needed(store=SESSION_STORE, legacy_file=LEGACY_SESSIONS_FILE, logger=logger)
    sessions = load_sessions()

    async def runner():
        core = ManagerCore(
            registry,
            task_timeout_s=args.timeout,
            base_dir=BASE_DIR,
            logger=logger,
            node_notify_chat_ids=node_notify_chat_ids,
            node_notify_min_interval_s=node_notify_min_interval_s,
        )
        ws_task = asyncio.create_task(run_ws_server(args.ws_listen, registry, core, logger=logger), name="ws_server")
        ctl_task: asyncio.Task | None = None
        if args.control_listen:
            if not args.control_token:
                raise SystemExit("control server enabled but missing --control-token (or CODEX_MANAGER_CONTROL_TOKEN)")
            ctl_task = asyncio.create_task(
                run_control_server(
                    args.control_listen,
                    args.control_token,
                    registry=registry,
                    core=core,
                    session_store=SESSION_STORE,
                    base_dir=BASE_DIR,
                    logger=logger,
                ),
                name="control_server",
            )
        stop_event = asyncio.Event()

        def _stop() -> None:
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                pass

        # Debug aid: dump stack traces on SIGUSR1 to diagnose hangs in the field.
        # Usage: kill -USR1 <pid> (see journalctl for the dump)
        try:
            faulthandler.enable()
            faulthandler.register(signal.SIGUSR1, all_threads=True)
            logger.info("faulthandler enabled (SIGUSR1 dumps stacks)")
        except Exception as e:
            logger.warning(f"faulthandler setup failed: {type(e).__name__}: {e}")

        dispatch_node = args.dispatch_node
        if dispatch_node:
            # WS-only probe mode: wait for node then dispatch once and exit.
            deadline = time.time() + max(5.0, args.timeout)
            while time.time() < deadline:
                if registry.is_online(dispatch_node):
                    break
                await asyncio.sleep(0.2)
            if not registry.is_online(dispatch_node):
                raise SystemExit(f"node not online: {dispatch_node}")
            res = await core.dispatch_once(dispatch_node, args.prompt or "ping", timeout_s=args.timeout)
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

        public_ws = _resolve_manager_public_ws(ws_listen=args.ws_listen, cfg=cfg, logger=logger)

        app = ManagerApp(
            core=core,
            registry=registry,
            sessions=sessions,
            node_auth=node_auth,
            tg_auth=tg_auth,
            default_node=default_node,
            manager_public_ws=public_ws,
            task_timeout_s=args.timeout,
            ws_listen=args.ws_listen,
            control_listen=args.control_listen,
            git_sha=git_sha,
            started_at_ts=time.time(),
        )
        async def telegram_loop() -> None:
            # Keep WS registry alive even if Telegram is temporarily unreachable.
            backoff_s = 2.0
            while not stop_event.is_set():
                tg = None
                try:
                    # Telegram 代理策略：
                    # - 只使用系统环境变量 HTTP_PROXY / HTTPS_PROXY / NO_PROXY
                    # - 不再读取 TELEGRAM_PROXY / manager_config.json 里的 telegram_proxy（避免多处配置互相打架）
                    proxy = None
                    trust_env = True
                    logger.info("Telegram http: proxy=<env> trust_env=true")
                    # PTB long-polling (getUpdates) can occupy connections for a long time.
                    # If we share the same httpx pool for both getUpdates and send/edit calls,
                    # we can hit:
                    #   "Pool timeout: All connections in the connection pool are occupied."
                    # Fix: use dedicated request clients for:
                    # - API calls (send/edit/delete webhook)
                    # - getUpdates long-polling
                    req_api = HTTPXRequest(
                        connection_pool_size=64,
                        # connect timeout should be short; retries handle transient proxy issues.
                        connect_timeout=10.0,
                        read_timeout=120.0,
                        write_timeout=60.0,
                        pool_timeout=180.0,
                        proxy=proxy,
                        httpx_kwargs={"trust_env": trust_env},
                    )
                    req_updates = HTTPXRequest(
                        connection_pool_size=32,
                        connect_timeout=10.0,
                        # getUpdates is long-polling. read_timeout must be > polling timeout.
                        read_timeout=180.0,
                        write_timeout=60.0,
                        pool_timeout=180.0,
                        proxy=proxy,
                        httpx_kwargs={"trust_env": trust_env},
                    )
                    tg = (
                        Application.builder()
                        .token(bot_token)
                        .request(req_api)
                        .get_updates_request(req_updates)
                        .build()
                    )
                    tg.bot_data["core"] = core
                    tg.add_handler(CommandHandler("help", app._with_typing(app.cmd_help, what="/help typing")))
                    tg.add_handler(CommandHandler("start", app._with_typing(app.cmd_start, what="/start typing")))
                    tg.add_handler(CommandHandler("ping", app._with_typing(app.cmd_ping, what="/ping typing")))
                    tg.add_handler(CommandHandler("approve", app._with_typing(app.cmd_approve, what="/approve typing")))
                    tg.add_handler(CommandHandler("decline", app._with_typing(app.cmd_decline, what="/decline typing")))
                    tg.add_handler(CallbackQueryHandler(app._with_typing(app.on_approve_callback, what="approve callback typing"), pattern=r"^approve:"))
                    tg.add_handler(CommandHandler("node", app._with_typing(app.cmd_node, what="/node typing")))
                    tg.add_handler(CommandHandler("manager", app._with_typing(app.cmd_manager, what="/manager typing")))
                    tg.add_handler(CallbackQueryHandler(app._with_typing(app.on_node_callback, what="node callback typing"), pattern=r"^node:"))
                    tg.add_handler(CommandHandler("status", app._with_typing(app.cmd_status, what="/status typing")))
                    tg.add_handler(CommandHandler("model", app._with_typing(app.cmd_model, what="/model typing")))
                    tg.add_handler(CommandHandler("token", app._with_typing(app.cmd_token, what="/token typing")))
                    tg.add_handler(CallbackQueryHandler(app._with_typing(app.on_model_callback, what="model callback typing"), pattern=r"^model:"))
                    tg.add_handler(CallbackQueryHandler(app._with_typing(app.on_effort_callback, what="effort callback typing"), pattern=r"^effort:"))
                    tg.add_handler(CommandHandler("result", app._with_typing(app.cmd_result, what="/result typing")))
                    tg.add_handler(CommandHandler("thread", app._with_typing(app.cmd_thread, what="/thread typing")))
                    tg.add_handler(CallbackQueryHandler(app._with_typing(app.on_thread_callback, what="thread callback typing"), pattern=r"^thread:"))
                    tg.add_handler(CommandHandler("skills", app._with_typing(app.cmd_skills, what="/skills typing")))
                    tg.add_handler(CommandHandler("config", app._with_typing(app.cmd_config, what="/config typing")))
                    tg.add_handler(CommandHandler("collaborationmode", app._with_typing(app.cmd_collaborationmode, what="/collaborationmode typing")))
                    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, app._with_typing(app.on_text, what="text typing")))
                    async def _on_tg_error(_update: object, context: object) -> None:
                        err = getattr(context, "error", None)
                        logger.warning(f"telegram handler error: {err!r}")

                    tg.add_error_handler(_on_tg_error, block=False)

                    logger.info("Telegram init: begin")
                    await asyncio.wait_for(tg.initialize(), timeout=30.0)
                    logger.info("Telegram init: ok")
                    # Ensure we're in polling mode. If a webhook is set, getUpdates won't deliver anything.
                    try:
                        logger.info("Telegram delete_webhook: begin")
                        await asyncio.wait_for(tg.bot.delete_webhook(drop_pending_updates=False), timeout=20.0)
                        logger.info("Telegram delete_webhook: ok")
                    except Exception as e:
                        logger.warning(f"delete_webhook failed: {type(e).__name__}: {e}")
                    logger.info("Telegram start: begin")
                    await asyncio.wait_for(tg.start(), timeout=30.0)
                    logger.info("Telegram start: ok")
                    core.set_outbox(TelegramOutbox(tg.bot, tg_call=_tg_call, logger=logger))
                    assert tg.updater is not None
                    logger.info("Telegram polling: begin")
                    await asyncio.wait_for(
                        tg.updater.start_polling(drop_pending_updates=False, timeout=60, allowed_updates=Update.ALL_TYPES),
                        timeout=30.0,
                    )
                    logger.info("Telegram polling started")
                    if startup_notify_chat_ids:
                        online = registry.online_node_ids()
                        text_lines = []
                        text_lines.append("codex_manager 已启动")
                        text_lines.append(f"time: {datetime.now().isoformat(timespec='seconds')}")
                        text_lines.append(f"host: {os.uname().nodename}")
                        if git_sha:
                            text_lines.append(f"git: {git_sha}")
                        text_lines.append(f"ws_listen: {args.ws_listen}")
                        if args.control_listen:
                            text_lines.append(f"control_listen: {args.control_listen}")
                        text_lines.append(f"nodes_online: {', '.join(online) if online else '(none)'}")
                        text_lines.append("tips: /help, /node, /status, /model")

                        # Startup helper: show a ready-to-copy node_config.json template.
                        # This is best-effort because ws_listen may be 0.0.0.0 and the real reachable
                        # address might be a public IP / VPN IP.
                        public_ws = _resolve_manager_public_ws(ws_listen=args.ws_listen, cfg=cfg, logger=logger)

                        shared_token = (os.environ.get("AGENT_NODE_TOKEN") or str(cfg.get("agent_node_token") or "")).strip()
                        if not shared_token:
                            shared_token = "<NODE_TOKEN>"

                        node_tpl = {
                            "manager_ws": public_ws,
                            "node_id": "<node_id>",
                            "node_token": shared_token,
                            "max_pending": 10,
                            "sandbox": "dangerFullAccess",
                            "approval_policy": "onRequest",
                            "codex_cwd": "/root/telegram-bot",
                            "codex_bin": "codex",
                        }
                        text_lines.append("")
                        text_lines.append("node_config.json 模板（复制到 node 机器的 /root/telegram-bot/node_config.json）：")
                        text_lines.append("```json")
                        text_lines.append(json.dumps(node_tpl, ensure_ascii=False, indent=2))
                        text_lines.append("```")
                        msg_text = "\n".join(text_lines)
                        for chat_id in startup_notify_chat_ids:
                            try:
                                await _tg_call(lambda: tg.bot.send_message(chat_id=chat_id, text=msg_text), timeout_s=15.0, what="startup_notify")
                                logger.info(f"op=startup_notify ok=true chat_id={chat_id}")
                            except Exception as e:
                                logger.warning(f"op=startup_notify ok=false chat_id={chat_id} error={type(e).__name__}: {e}")
                    backoff_s = 2.0
                    await stop_event.wait()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Avoid leaking Telegram bot token in logs. python-telegram-bot may include the token
                    # in InvalidToken error messages.
                    msg = f"{type(e).__name__}: {e}"
                    if "InvalidToken" in msg and "The token" in msg:
                        # Example: "InvalidToken: The token `123:ABC...` was rejected by the server."
                        msg = re.sub(r"(The token\s+)(`[^`]+`|'[^']+'|\S+)(\s+was rejected)", r"\1<redacted>\3", msg)
                    logger.warning(f"Telegram start failed: {msg} (retry in {backoff_s:.1f}s)")
                    await asyncio.sleep(backoff_s)
                    backoff_s = min(30.0, backoff_s * 1.7)
                finally:
                    if tg is not None:
                        try:
                            if tg.updater is not None:
                                await asyncio.wait_for(tg.updater.stop(), timeout=5.0)
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(tg.stop(), timeout=5.0)
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(tg.shutdown(), timeout=5.0)
                        except Exception:
                            pass

        tg_task = asyncio.create_task(telegram_loop(), name="telegram_loop")
        drain_timeout_s = float(os.environ.get("CODEX_MANAGER_DRAIN_TIMEOUT", str(cfg.get("manager_drain_timeout_s") or 45.0)) or 45.0)
        try:
            await stop_event.wait()
        finally:
            logger.info(f"shutdown requested; begin drain timeout_s={drain_timeout_s:.1f}")
            try:
                await core.begin_drain()
                pending = await core.wait_drained(drain_timeout_s)
                logger.info(f"drain finished pending={pending}")
            except Exception as e:
                logger.warning(f"drain failed: {type(e).__name__}: {e}")
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
