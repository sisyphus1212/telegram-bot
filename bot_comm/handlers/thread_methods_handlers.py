from __future__ import annotations

from typing import Any, Callable

from telegram import Update
from telegram.ext import ContextTypes


class ThreadMethodsHandlers:
    def __init__(
        self,
        *,
        is_allowed: Callable[[Update], bool],
        tg_call: Callable[..., Any],
        parse_kv: Callable[[list[str]], dict[str, str]],
        require_node_online: Callable[[Update], Any],
        session_key_fn: Callable[[Update], str],
        get_current_thread_id: Callable[[str, str], str],
        set_current_thread_id: Callable[[str, str, str], None],
        get_default_model: Callable[[str], str],
        save_sessions_fn: Callable[[dict[str, dict]], None],
        sessions_ref: dict[str, dict],
        pretty_json: Callable[[Any], str],
        task_timeout_s: float,
        logger: Any,
    ) -> None:
        self.is_allowed = is_allowed
        self.tg_call = tg_call
        self.parse_kv = parse_kv
        self.require_node_online = require_node_online
        self.session_key_fn = session_key_fn
        self.get_current_thread_id = get_current_thread_id
        self.set_current_thread_id = set_current_thread_id
        self.get_default_model = get_default_model
        self.save_sessions_fn = save_sessions_fn
        self.sessions_ref = sessions_ref
        self.pretty_json = pretty_json
        self.task_timeout_s = task_timeout_s
        self.logger = logger

    async def cmd_thread_current(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /thread_current chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        msg = update.effective_message
        if msg is None:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: msg.reply_text("unauthorized"), timeout_s=15.0, what="/thread_current reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        sk = self.session_key_fn(update)
        tid = self.get_current_thread_id(sk, node_id)
        await self.tg_call(lambda: msg.reply_text(f"threadId: {tid or '(none)'}"), timeout_s=15.0, what="/thread_current reply")

    async def cmd_thread_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /thread_start chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        msg = update.effective_message
        if msg is None:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: msg.reply_text("unauthorized"), timeout_s=15.0, what="/thread_start reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        kv = self.parse_kv(context.args or [])
        params: dict[str, Any] = {}
        for k in ("cwd", "sandbox", "approvalPolicy", "personality", "model", "baseInstructions"):
            if k in kv:
                params[k] = kv[k]
        sk = self.session_key_fn(update)
        if "model" not in params:
            default_model = self.get_default_model(sk)
            if default_model:
                params["model"] = default_model
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: msg.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/thread_start reply")
            return
        rep = await core.appserver_call(node_id, "thread/start", params, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await self.tg_call(lambda: msg.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_start reply")
            return
        result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            await self.tg_call(lambda: msg.reply_text(f"[{node_id}] error: thread/start missing id"), timeout_s=15.0, what="/thread_start reply")
            return
        self.set_current_thread_id(sk, node_id, thread_id)
        self.save_sessions_fn(self.sessions_ref)
        await self.tg_call(lambda: msg.reply_text(f"[{node_id}] ok threadId={thread_id}"), timeout_s=15.0, what="/thread_start reply")

    async def cmd_thread_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /thread_resume chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_resume reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        kv = self.parse_kv(context.args or [])
        thread_id = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        if not thread_id:
            await self.tg_call(lambda: update.message.reply_text("usage: /thread_resume <id>"), timeout_s=15.0, what="/thread_resume reply")
            return
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/thread_resume reply")
            return
        rep = await core.appserver_call(node_id, "thread/resume", {"threadId": thread_id}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_resume reply")
            return
        sk = self.session_key_fn(update)
        self.set_current_thread_id(sk, node_id, thread_id)
        self.save_sessions_fn(self.sessions_ref)
        await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] ok threadId={thread_id}"), timeout_s=15.0, what="/thread_resume reply")

    async def cmd_thread_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /thread_list chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        msg = update.effective_message
        if msg is None:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: msg.reply_text("unauthorized"), timeout_s=15.0, what="/thread_list reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        kv = self.parse_kv(context.args or [])
        params: dict[str, Any] = {}
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
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: msg.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/thread_list reply")
            return
        rep = await core.appserver_call(node_id, "thread/list", params, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await self.tg_call(lambda: msg.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_list reply")
            return
        result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
        data = result.get("data") if isinstance(result.get("data"), list) else []
        lines: list[str] = [f"node: {node_id}", f"count: {len(data)}"]
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
        await self.tg_call(lambda: msg.reply_text("\n".join(lines)), timeout_s=15.0, what="/thread_list reply")

    async def cmd_thread_read(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /thread_read chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_read reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        kv = self.parse_kv(context.args or [])
        thread_id = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        if not thread_id:
            await self.tg_call(lambda: update.message.reply_text("usage: /thread_read <id> includeTurns=false"), timeout_s=15.0, what="/thread_read reply")
            return
        include_turns = str(kv.get("includeTurns") or "false").lower() in ("1", "true", "yes", "y", "on")
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/thread_read reply")
            return
        rep = await core.appserver_call(node_id, "thread/read", {"threadId": thread_id, "includeTurns": include_turns}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_read reply")
            return
        await self.tg_call(lambda: update.message.reply_text(self.pretty_json(rep.get("result"))), timeout_s=15.0, what="/thread_read reply")

    async def cmd_thread_archive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /thread_archive chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_archive reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        kv = self.parse_kv(context.args or [])
        thread_id = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        if not thread_id:
            sk = self.session_key_fn(update)
            thread_id = self.get_current_thread_id(sk, node_id)
        if not thread_id:
            await self.tg_call(lambda: update.message.reply_text("usage: /thread_archive threadId=<id> (or set current thread first)"), timeout_s=15.0, what="/thread_archive reply")
            return
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/thread_archive reply")
            return
        rep = await core.appserver_call(node_id, "thread/archive", {"threadId": thread_id}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_archive reply")
            return
        await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] ok archived threadId={thread_id}"), timeout_s=15.0, what="/thread_archive reply")

    async def cmd_thread_unarchive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /thread_unarchive chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/thread_unarchive reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        kv = self.parse_kv(context.args or [])
        thread_id = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        if not thread_id:
            await self.tg_call(lambda: update.message.reply_text("usage: /thread_unarchive <id>"), timeout_s=15.0, what="/thread_unarchive reply")
            return
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/thread_unarchive reply")
            return
        rep = await core.appserver_call(node_id, "thread/unarchive", {"threadId": thread_id}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/thread_unarchive reply")
            return
        await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] ok unarchived threadId={thread_id}"), timeout_s=15.0, what="/thread_unarchive reply")
