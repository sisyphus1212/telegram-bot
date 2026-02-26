import os
import sys
import logging
import json
import socket
import asyncio
import subprocess
import re
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import log_rotate
from codex_app_server_client import CodexAppServerConfig, CodexAppServerProcess, CodexAppServerError


def _enable_proxy_if_available() -> bool:
    proxy_host = "127.0.0.1"
    proxy_port = 10809
    try:
        with socket.create_connection((proxy_host, proxy_port), timeout=1):
            os.environ["http_proxy"] = f"http://{proxy_host}:{proxy_port}"
            os.environ["https_proxy"] = f"http://{proxy_host}:{proxy_port}"
            os.environ["NO_PROXY"] = "127.0.0.1,localhost"
            return True
    except OSError:
        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        return False


PROXY_ENABLED = _enable_proxy_if_available()

if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="ignore")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="ignore")


LOG_DIR = Path(__file__).parent / "log"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "bot.log"
SESSIONS_FILE = Path(__file__).parent / "sessions.json"
CONFIG_FILE = Path(__file__).parent / "codex_config.json"
LEGACY_CONFIG_FILE = Path(__file__).parent / "opencode_config.json"


def setup_logging():
    log_rotate.rotate_logs(
        LOG_DIR,
        "bot.log",
        max_lines=5000,
        max_backups=3,
        archive_name_prefix="bot_log_archive",
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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    return logging.getLogger(__name__)


logger = setup_logging()
logger.info(f"Proxy enabled: {PROXY_ENABLED}")


def load_config() -> dict:
    cfg_path = CONFIG_FILE if CONFIG_FILE.exists() else LEGACY_CONFIG_FILE
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load config file {cfg_path}: {e}")
    return {}


_opencode_config = load_config()

# Hardcoded / config-backed chain:
# - Telegram -> this bot.py (polling)
# - this bot.py -> Codex CLI (`codex exec`) to establish/resume sessions and operate the PC.
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
# Default to this directory so `telegram/` can be extracted as a standalone project.
CODEX_CWD = os.environ.get("CODEX_CWD") or str(Path(__file__).resolve().parent)
CODEX_CLIENT_NAME = os.environ.get("CODEX_APP_SERVER_CLIENT_NAME", "telegram_bot")
CODEX_CLIENT_VERSION = os.environ.get("CODEX_APP_SERVER_CLIENT_VERSION", "0.0")

# For "just make it work", you can hardcode BOT_TOKEN here.
# Recommended: set TELEGRAM_BOT_TOKEN env var or put telegram_bot_token in codex_config.json.
BOT_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or _opencode_config.get("telegram_bot_token")
    or _opencode_config.get("bot_token")
    or ""
)

ALLOWED_USER_IDS = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
PC_MODE_DEFAULT = os.environ.get("TELEGRAM_PC_MODE_DEFAULT", "0").strip().lower() in ("1", "true", "yes", "on")

_UUID_RE = re.compile(r"^(?:urn:uuid:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _is_valid_thread_id(value: str) -> bool:
    return bool(value) and bool(_UUID_RE.fullmatch(value.strip()))


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


_ALLOWED_USER_IDS = _parse_allowed_user_ids(ALLOWED_USER_IDS)


def load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Backward compat: old format was {user_id: session_id}
                if isinstance(data, dict):
                    upgraded = {}
                    for k, v in data.items():
                        if isinstance(v, str):
                            # Old opencode ids look like "ses_..."; keep blank so we create a new Codex thread.
                            upgraded[k] = {"id": v if _is_valid_thread_id(v) else "", "pc_mode": PC_MODE_DEFAULT}
                        elif isinstance(v, dict):
                            raw_id = v.get("id") or v.get("session_id") or ""
                            upgraded[k] = {
                                "id": raw_id if isinstance(raw_id, str) and _is_valid_thread_id(raw_id) else "",
                                "pc_mode": bool(v.get("pc_mode", PC_MODE_DEFAULT)),
                            }
                        else:
                            upgraded[k] = {"id": "", "pc_mode": PC_MODE_DEFAULT}
                    return upgraded
                return {}
        except Exception as e:
            logger.error(f"加载会话失败: {e}")
    return {}


def save_sessions(sessions: dict) -> None:
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存会话失败: {e}")


def _session_key(update: Update) -> str:
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    return f"{chat_id}:{user_id}"


def _legacy_user_key(update: Update) -> str:
    # Backward-compat for old sessions.json that keyed by user_id only.
    user_id = update.effective_user.id if update.effective_user else 0
    return str(user_id)


def _is_user_allowed(update: Update) -> bool:
    if not _ALLOWED_USER_IDS:
        return True
    user_id = update.effective_user.id if update.effective_user else 0
    return user_id in _ALLOWED_USER_IDS


def _extract_text_from_parts(parts) -> str:
    texts: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text", "")
                if t:
                    texts.append(str(t))
    return "\n".join(texts).strip()


def _chunk_telegram(text: str, limit: int = 3900) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        chunk = text[:limit]
        cut = chunk.rfind("\n")
        if cut > 800:
            chunk = chunk[:cut]
        chunks.append(chunk)
        text = text[len(chunk) :]
    return chunks


PC_SESSION_PRIMER = """你是一个在本机运行的编程助手(通过 OpenCode/Codex Server)。
你可以根据用户指令建立/继续会话，并在需要时使用本机工具来完成任务(例如浏览器自动化、文件读写、命令执行等，具体取决于 server 已启用的工具)。
要求:
1) 先给出简短方案(步骤)，需要破坏性操作时先询问确认。
2) 默认不要泄露敏感信息(令牌/密码/私钥)；遇到这类信息只提示用户在本机配置。
3) 需要用户配合时，明确提出需要的输入。"""


def _build_user_prompt(user_text: str, pc_mode: bool) -> str:
    if not pc_mode:
        return user_text
    return (
        "[PC_MODE]\n"
        "目标: 按用户要求在本机环境完成任务，必要时使用已启用的工具。\n"
        "如果需要确认(删除/覆盖/执行未知脚本/安装软件/远程访问)先询问。\n"
        f"用户请求: {user_text}\n"
    )


def _codex_version_sync() -> str:
    try:
        out = subprocess.check_output([CODEX_BIN, "--version"], cwd=CODEX_CWD, stderr=subprocess.STDOUT)
        return out.decode("utf-8", errors="ignore").strip()
    except Exception as e:
        return f"codex --version failed: {e}"


async def _ensure_app_server(context: ContextTypes.DEFAULT_TYPE) -> CodexAppServerProcess:
    app_server: CodexAppServerProcess = context.application.bot_data.get("app_server")  # type: ignore[assignment]
    if not app_server:
        app_server = CodexAppServerProcess(CodexAppServerConfig(codex_bin=CODEX_BIN, cwd=CODEX_CWD), on_log=logger.info)
        context.application.bot_data["app_server"] = app_server
    await app_server.ensure_started_and_initialized(CODEX_CLIENT_NAME, CODEX_CLIENT_VERSION)
    return app_server


def log_message(update: Update, msg_type: str):
    user = update.effective_user
    text = update.message.text if update.message else ""
    logger.info(f"[收到] {msg_type} {user.first_name}({user.id}): {text}")
    print(f"[收到] {user.first_name}: {text}", flush=True)


def log_reply(text: str):
    logger.info(f"[回复] Codex: {text[:80]}...")
    print(f"[回复] Codex: {text}", flush=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_message(update, "/start")
    if not _is_user_allowed(update):
        await update.message.reply_text("未授权用户。")
        return
    reply = f"""你好 {update.effective_user.first_name}!

我是连接到 Codex App Server 的 Bot。直接发送消息，我会转发给本机 `codex app-server` 获取回复。

命令:
- /status 查看状态
- /reset 重置当前会话
- /pc on|off 切换PC模式(会提示 server 进行本机操作)
"""
    await update.message.reply_text(reply)
    log_reply(reply)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_message(update, "/help")
    if not _is_user_allowed(update):
        await update.message.reply_text("未授权用户。")
        return
    reply = """使用方法:
1) 直接发消息
2) Bot 转发给 Codex App Server
3) Codex 回复后转发回 Telegram
"""
    await update.message.reply_text(reply)
    log_reply(reply)


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_message(update, "/info")
    if not _is_user_allowed(update):
        await update.message.reply_text("未授权用户。")
        return
    user = update.effective_user
    key = _session_key(update)
    legacy_key = _legacy_user_key(update)
    session = sessions.get(key) or sessions.get(legacy_key, {})
    if isinstance(session, dict) and legacy_key in sessions and key not in sessions:
        sessions[key] = session
        sessions.pop(legacy_key, None)
        save_sessions(sessions)
    session_id = session.get("id") if isinstance(session, dict) else None
    pc_mode = session.get("pc_mode") if isinstance(session, dict) else None
    reply = f"""用户信息:
ID: {user.id}
名字: {user.first_name}
用户名: @{user.username if user.username else '未设置'}
会话Key: {key}
会话ID: {session_id if session_id else '无'}
PC模式: {pc_mode if pc_mode is not None else '未知'}
会话数: {len(sessions)}"""
    await update.message.reply_text(reply)
    log_reply(reply)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_message(update, "/status")
    if not _is_user_allowed(update):
        await update.message.reply_text("未授权用户。")
        return
    version = _codex_version_sync()
    app_server: CodexAppServerProcess | None = context.application.bot_data.get("app_server")  # type: ignore[assignment]
    running = bool(app_server and app_server.is_running())
    reply = f"""Bot状态:
运行状态: 正常
Codex: {version}
Codex CWD: {CODEX_CWD}
App Server: {'running' if running else 'not running (will start on demand)'}
会话数量: {len(sessions)}"""
    await update.message.reply_text(reply)
    log_reply(reply)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_message(update, "/reset")
    if not _is_user_allowed(update):
        await update.message.reply_text("未授权用户。")
        return
    key = _session_key(update)
    legacy_key = _legacy_user_key(update)
    if key in sessions:
        sessions.pop(key, None)
    if legacy_key in sessions:
        sessions.pop(legacy_key, None)
    save_sessions(sessions)
    await update.message.reply_text("已重置会话。下条消息会创建新会话。")


async def pc_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_message(update, "/pc")
    if not _is_user_allowed(update):
        await update.message.reply_text("未授权用户。")
        return
    key = _session_key(update)
    legacy_key = _legacy_user_key(update)
    session = sessions.get(key) or sessions.get(legacy_key) or {"id": "", "pc_mode": PC_MODE_DEFAULT}
    if not isinstance(session, dict):
        session = {"id": "", "pc_mode": PC_MODE_DEFAULT}
    if legacy_key in sessions and key not in sessions and isinstance(session, dict):
        sessions[key] = session
        sessions.pop(legacy_key, None)

    arg = (context.args[0] if getattr(context, "args", None) else "").strip().lower()
    if arg in ("on", "1", "true", "yes"):
        session["pc_mode"] = True
    elif arg in ("off", "0", "false", "no"):
        session["pc_mode"] = False
    else:
        session["pc_mode"] = not bool(session.get("pc_mode", False))

    # PC mode changes sandbox/approval expectations; reset thread to avoid mixing policies.
    session["id"] = ""
    sessions[key] = session
    save_sessions(sessions)
    await update.message.reply_text(f"PC模式: {session['pc_mode']}")


async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_message(update, "消息")
    if not _is_user_allowed(update):
        await update.message.reply_text("未授权用户。")
        return

    key = _session_key(update)
    legacy_key = _legacy_user_key(update)
    app_server = await _ensure_app_server(context)

    try:
        session = sessions.get(key) or sessions.get(legacy_key)
        if not isinstance(session, dict):
            session = {"id": "", "pc_mode": PC_MODE_DEFAULT}
        if legacy_key in sessions and key not in sessions and isinstance(session, dict):
            sessions[key] = session
            sessions.pop(legacy_key, None)
            save_sessions(sessions)

        if not session.get("id"):
            logger.info(f"为 {key} 创建 Codex thread")
            session.setdefault("pc_mode", PC_MODE_DEFAULT)
            sessions[key] = session
            save_sessions(sessions)

        pc_mode_enabled = bool(session.get("pc_mode", False))

        user_message = update.message.text or ""
        prompt = _build_user_prompt(user_message, pc_mode_enabled)

        thread_id = session.get("id") or ""
        if thread_id and not _is_valid_thread_id(thread_id):
            # Old opencode session ids look like `ses_...` and are not valid Codex thread ids.
            thread_id = ""
            session["id"] = ""
            sessions[key] = session
            save_sessions(sessions)

        if not thread_id:
            # For stability, keep PC-mode "danger-full-access" + "never" (no approval prompts).
            sandbox = "danger-full-access" if pc_mode_enabled else "workspace-write"
            approval = "never" if pc_mode_enabled else "on-request"
            base_instructions = PC_SESSION_PRIMER if pc_mode_enabled else None
            thread_id = await app_server.thread_start(
                cwd=CODEX_CWD,
                sandbox=sandbox,
                approval_policy=approval,
                personality="pragmatic",
                base_instructions=base_instructions,
            )
            session["id"] = thread_id
            sessions[key] = session
            save_sessions(sessions)

        async def _run_once(tid: str) -> str:
            logger.info(f"turn/start thread={tid} pc_mode={pc_mode_enabled} msg={user_message[:200]!r}")
            turn_id = await app_server.turn_start_text(thread_id=tid, text=prompt)
            return await app_server.run_turn_and_collect_agent_message(thread_id=tid, turn_id=turn_id)

        try:
            reply_text = await _run_once(thread_id)
        except CodexAppServerError as e:
            # If we somehow persisted a bad thread id, recover by creating a new thread and retry once.
            msg = str(e)
            if "invalid thread id" in msg:
                session["id"] = ""
                sessions[key] = session
                save_sessions(sessions)

                sandbox = "danger-full-access" if pc_mode_enabled else "workspace-write"
                approval = "never" if pc_mode_enabled else "on-request"
                base_instructions = PC_SESSION_PRIMER if pc_mode_enabled else None
                thread_id = await app_server.thread_start(
                    cwd=CODEX_CWD,
                    sandbox=sandbox,
                    approval_policy=approval,
                    personality="pragmatic",
                    base_instructions=base_instructions,
                )
                session["id"] = thread_id
                sessions[key] = session
                save_sessions(sessions)
                reply_text = await _run_once(thread_id)
            else:
                raise

        logger.info(f"Codex 回复: {reply_text[:80]}...")
        for chunk in _chunk_telegram(reply_text or "(empty reply)"):
            await update.message.reply_text(chunk)
        log_reply(reply_text)

    except (CodexAppServerError, Exception) as e:
        logger.error(f"处理错误: {e}", exc_info=True)
        error_msg = f"处理失败: {str(e)}"
        await update.message.reply_text(error_msg)
        log_reply(error_msg)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update and update.error:
        logger.error(f"更新错误: {update.error}", exc_info=True)
    else:
        logger.error("未知错误", exc_info=True)


def main():
    global sessions
    sessions = load_sessions()

    logger.info("=" * 50)
    logger.info("Bot 启动中...")
    logger.info(f"Codex: {CODEX_BIN} (cwd={CODEX_CWD})")
    logger.info(f"日志文件: {LOG_FILE}")
    logger.info(f"会话文件: {SESSIONS_FILE}")
    logger.info("=" * 50)

    print("=" * 50)
    print("Bot 启动中...")
    print(f"Codex: {CODEX_BIN} (cwd={CODEX_CWD})")
    print("=" * 50)

    if not BOT_TOKEN:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN. Set env TELEGRAM_BOT_TOKEN or put telegram_bot_token in codex_config.json"
        )

    builder = Application.builder().token(BOT_TOKEN)

    async def _post_init(app: Application) -> None:
        # Start app-server early so the first user message doesn't pay startup cost.
        app_server = CodexAppServerProcess(
            CodexAppServerConfig(codex_bin=CODEX_BIN, cwd=CODEX_CWD),
            on_log=logger.info,
        )
        app.bot_data["app_server"] = app_server
        await app_server.ensure_started_and_initialized(CODEX_CLIENT_NAME, CODEX_CLIENT_VERSION)

    async def _post_shutdown(app: Application) -> None:
        app_server: CodexAppServerProcess | None = app.bot_data.get("app_server")  # type: ignore[assignment]
        if app_server:
            await app_server.stop()

    # Best-effort hooks (some PTB versions may not support these).
    try:
        builder = builder.post_init(_post_init)  # type: ignore[attr-defined]
        builder = builder.post_shutdown(_post_shutdown)  # type: ignore[attr-defined]
    except Exception:
        pass

    application = builder.build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("info", info))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("pc", pc_mode))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_chat))
    application.add_error_handler(error_handler)

    print("Bot 正在运行... (按 Ctrl+C 停止)")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("Bot 正在停止...")
        save_sessions(sessions)
        logger.info("Bot 已停止")
    except Exception as e:
        logger.critical(f"Bot 崩溃: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
