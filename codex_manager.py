import argparse
import asyncio
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
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


class ProxyRegistry:
    def __init__(self, allowed: dict[str, str]) -> None:
        self._allowed = allowed  # proxy_id -> token
        self._lock = asyncio.Lock()
        self._conns: dict[str, ProxyConn] = {}
        self._tasks_by_proxy: dict[str, set[str]] = {}
        self._task_futures: dict[str, asyncio.Future[JsonDict]] = {}

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
            self._conns[proxy_id] = ProxyConn(proxy_id=proxy_id, ws=ws, last_seen=time.time())
            self._tasks_by_proxy.setdefault(proxy_id, set())
        return True

    async def unregister_if_matches(self, proxy_id: str, ws: Any) -> None:
        async with self._lock:
            cur = self._conns.get(proxy_id)
            if not cur or cur.ws is not ws:
                return
            self._conns.pop(proxy_id, None)
            task_ids = self._tasks_by_proxy.pop(proxy_id, set())
            for tid in task_ids:
                fut = self._task_futures.pop(tid, None)
                if fut and not fut.done():
                    fut.set_exception(RuntimeError(f"proxy disconnected: {proxy_id}"))

    async def heartbeat(self, proxy_id: str, ws: Any) -> None:
        async with self._lock:
            cur = self._conns.get(proxy_id)
            if cur and cur.ws is ws:
                cur.last_seen = time.time()

    async def deliver_task_result(self, proxy_id: str, ws: Any, msg: JsonDict) -> None:
        task_id = msg.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return
        async with self._lock:
            cur = self._conns.get(proxy_id)
            if not cur or cur.ws is not ws:
                return
            fut = self._task_futures.pop(task_id, None)
            if fut and not fut.done():
                fut.set_result(msg)
            self._tasks_by_proxy.get(proxy_id, set()).discard(task_id)

    async def dispatch(self, proxy_id: str, task_msg: JsonDict, timeout_s: float) -> JsonDict:
        task_id = task_msg.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError("missing task_id")

        async with self._lock:
            conn = self._conns.get(proxy_id)
            if not conn:
                raise RuntimeError(f"proxy offline: {proxy_id}")
            ws = conn.ws
            fut: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
            self._task_futures[task_id] = fut
            self._tasks_by_proxy.setdefault(proxy_id, set()).add(task_id)

        try:
            await ws.send(json.dumps(task_msg, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            async with self._lock:
                self._task_futures.pop(task_id, None)
                self._tasks_by_proxy.get(proxy_id, set()).discard(task_id)
            raise

        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except Exception:
            async with self._lock:
                self._task_futures.pop(task_id, None)
                self._tasks_by_proxy.get(proxy_id, set()).discard(task_id)
            raise


async def run_ws_server(listen: str, registry: ProxyRegistry) -> None:
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
                if t == "task_result":
                    await registry.deliver_task_result(proxy_id, ws, m)
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
        if not self._is_allowed(update):
            await update.message.reply_text("unauthorized")
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
        await update.message.reply_text("\n".join(lines))

    async def cmd_use(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await update.message.reply_text("unauthorized")
            return
        sk = _session_key(update)
        if not context.args:
            await update.message.reply_text("usage: /use <proxy_id>")
            return
        proxy_id = str(context.args[0]).strip()
        if not proxy_id:
            await update.message.reply_text("usage: /use <proxy_id>")
            return
        if not self.registry.is_online(proxy_id):
            await update.message.reply_text(f"proxy offline: {proxy_id}")
            return
        sess = self.sessions.get(sk) or {"proxy": "", "pc_mode": False, "reset_next": False}
        sess["proxy"] = proxy_id
        sess["reset_next"] = True  # switch machine => reset remote thread mapping
        self.sessions[sk] = sess
        save_sessions(self.sessions)
        await update.message.reply_text(f"using proxy: {proxy_id} (next turn will reset)")

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await update.message.reply_text("unauthorized")
            return
        sk = _session_key(update)
        sess = self.sessions.get(sk) or {"proxy": "", "pc_mode": False, "reset_next": False}
        sess["reset_next"] = True
        self.sessions[sk] = sess
        save_sessions(self.sessions)
        await update.message.reply_text("ok (next turn will reset)")

    async def cmd_pc(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await update.message.reply_text("unauthorized")
            return
        sk = _session_key(update)
        if not context.args or context.args[0] not in ("on", "off"):
            await update.message.reply_text("usage: /pc on|off")
            return
        mode = context.args[0] == "on"
        sess = self.sessions.get(sk) or {"proxy": "", "pc_mode": False, "reset_next": False}
        sess["pc_mode"] = mode
        self.sessions[sk] = sess
        save_sessions(self.sessions)
        await update.message.reply_text(f"pc_mode={mode} (currently not used by protocol)")

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await update.message.reply_text("unauthorized")
            return
        if not update.message or not isinstance(update.message.text, str):
            return

        sk = _session_key(update)
        sess = self.sessions.get(sk) or {"proxy": "", "pc_mode": False, "reset_next": False}
        proxy_id = self._choose_proxy(str(sess.get("proxy") or ""))
        if not proxy_id:
            await update.message.reply_text("no proxy online. use /servers")
            return

        task_id = uuid.uuid4().hex
        reset_thread = bool(sess.get("reset_next", False))
        sess["reset_next"] = False
        self.sessions[sk] = sess
        save_sessions(self.sessions)

        prompt = update.message.text
        placeholder = await update.message.reply_text(f"working (proxy={proxy_id}, reset={reset_thread}) ...")

        task_msg: JsonDict = {
            "type": "task_assign",
            "task_id": task_id,
            "thread_key": sk,
            "prompt": prompt,
            "reset_thread": reset_thread,
        }

        try:
            res = await self.registry.dispatch(proxy_id=proxy_id, task_msg=task_msg, timeout_s=self.task_timeout_s)
        except Exception as e:
            await placeholder.edit_text(f"error: {e}")
            return

        if not isinstance(res, dict) or res.get("type") != "task_result":
            await placeholder.edit_text("error: bad task_result")
            return

        if not res.get("ok"):
            err = res.get("error") or "unknown error"
            await placeholder.edit_text(f"error: {err}")
            return

        text = str(res.get("text") or "").strip()
        if not text:
            text = "(empty)"
        parts = _split_telegram_text(text)
        await placeholder.edit_text(parts[0])
        for extra in parts[1:]:
            await update.message.reply_text(extra)


def main() -> int:
    cfg = _load_config()

    ap = argparse.ArgumentParser()
    ap.add_argument("--ws-listen", default=os.environ.get("CODEX_MANAGER_WS_LISTEN") or str(cfg.get("manager_ws_listen") or "0.0.0.0:8765"))
    ap.add_argument("--ws-only", action="store_true", help="run WS server only (no Telegram)")
    ap.add_argument("--dispatch-proxy", default="", help="WS-only helper: wait for proxy online then dispatch a prompt once and exit")
    ap.add_argument("--prompt", default="", help="WS-only helper prompt")
    ap.add_argument("--timeout", type=float, default=float(os.environ.get("CODEX_TASK_TIMEOUT", cfg.get("task_timeout_s") or 120.0)))
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
        ws_task = asyncio.create_task(run_ws_server(args.ws_listen, registry), name="ws_server")
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
            task_id = uuid.uuid4().hex
            msg = {
                "type": "task_assign",
                "task_id": task_id,
                "thread_key": "probe:local",
                "prompt": args.prompt or "ping",
                "reset_thread": True,
            }
            res = await registry.dispatch(args.dispatch_proxy, msg, timeout_s=args.timeout)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            stop_event.set()

        if args.ws_only or not bot_token:
            if not bot_token and not args.ws_only:
                logger.warning("Missing TELEGRAM_BOT_TOKEN; running WS-only.")
            await stop_event.wait()
            ws_task.cancel()
            await asyncio.gather(ws_task, return_exceptions=True)
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
                    req = HTTPXRequest(connect_timeout=20.0, read_timeout=60.0, write_timeout=60.0, pool_timeout=20.0)
                    tg = Application.builder().token(bot_token).request(req).build()
                    tg.add_handler(CommandHandler("servers", app.cmd_servers))
                    tg.add_handler(CommandHandler("use", app.cmd_use))
                    tg.add_handler(CommandHandler("reset", app.cmd_reset))
                    tg.add_handler(CommandHandler("pc", app.cmd_pc))
                    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, app.on_text))

                    await tg.initialize()
                    await tg.start()
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
            await asyncio.gather(tg_task, ws_task, return_exceptions=True)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
