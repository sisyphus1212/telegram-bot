from __future__ import annotations

from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
        get_default_effort: Callable[[str], str],
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
        self.get_default_effort = get_default_effort
        self.save_sessions_fn = save_sessions_fn
        self.sessions_ref = sessions_ref
        self.pretty_json = pretty_json
        self.task_timeout_s = task_timeout_s
        self.logger = logger

    def _get_thread_index_map(self, sk: str, node_id: str) -> list[str]:
        sess = self.sessions_ref.get(sk)
        if not isinstance(sess, dict):
            return []
        by_node = sess.get("by_node") if isinstance(sess.get("by_node"), dict) else {}
        entry = by_node.get(node_id) if isinstance(by_node.get(node_id), dict) else {}
        arr = entry.get("thread_list_index") if isinstance(entry.get("thread_list_index"), list) else []
        out: list[str] = []
        for x in arr:
            s = str(x or "").strip()
            if s:
                out.append(s)
        return out

    def _set_thread_index_map(self, sk: str, node_id: str, ids: list[str]) -> None:
        sess = self.sessions_ref.setdefault(sk, {"node": node_id, "by_node": {}, "defaults": {}})
        if not isinstance(sess, dict):
            return
        by_node = sess.get("by_node")
        if not isinstance(by_node, dict):
            by_node = {}
            sess["by_node"] = by_node
        entry = by_node.get(node_id)
        if not isinstance(entry, dict):
            entry = {}
            by_node[node_id] = entry
        entry["thread_list_index"] = ids[:50]

    def _get_fork_wizard(self, sk: str, node_id: str) -> dict[str, Any]:
        sess = self.sessions_ref.setdefault(sk, {"node": node_id, "by_node": {}, "defaults": {}})
        by_node = sess.setdefault("by_node", {}) if isinstance(sess, dict) else {}
        entry = by_node.setdefault(node_id, {}) if isinstance(by_node, dict) else {}
        wiz = entry.get("fork_wizard")
        if not isinstance(wiz, dict):
            wiz = {}
            entry["fork_wizard"] = wiz
        return wiz

    def _clear_fork_wizard(self, sk: str, node_id: str) -> None:
        sess = self.sessions_ref.get(sk)
        if not isinstance(sess, dict):
            return
        by_node = sess.get("by_node") if isinstance(sess.get("by_node"), dict) else {}
        entry = by_node.get(node_id) if isinstance(by_node.get(node_id), dict) else {}
        if "fork_wizard" in entry:
            entry.pop("fork_wizard", None)

    def _build_fork_keyboard(self, *, sandbox: str, approval: str) -> InlineKeyboardMarkup:
        sb_vals = [("workspace-write", "workspace"), ("read-only", "readonly"), ("danger-full-access", "danger")]
        ap_vals = [("on-request", "onRequest"), ("never", "never")]
        row1 = [InlineKeyboardButton(("• " if sandbox == v else "") + lab, callback_data=f"thread:fork:sandbox:{v}") for v, lab in sb_vals]
        row2 = [InlineKeyboardButton(("• " if approval == v else "") + lab, callback_data=f"thread:fork:approval:{v}") for v, lab in ap_vals]
        row3 = [
            InlineKeyboardButton("Create Fork", callback_data="thread:fork:create"),
            InlineKeyboardButton("Cancel", callback_data="thread:fork:cancel"),
        ]
        return InlineKeyboardMarkup([row1, row2, row3])

    def _render_fork_text(self, *, node_id: str, source_tid: str, cwd: str, sandbox: str, approval: str) -> str:
        return "\n".join(
            [
                f"[{node_id}] thread fork",
                f"source: {source_tid}",
                f"cwd: {cwd or '(missing)'}",
                f"sandbox: {sandbox}",
                f"approval: {approval}",
                "",
                "用法: /thread fork <idx|threadId> cwd=/path",
                "然后点按钮选择权限并 Create Fork",
            ]
        )

    async def _resolve_thread_token(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        node_id: str,
        token: str,
        usage: str,
    ) -> str:
        token = (token or "").strip()
        if not token:
            return ""
        if token.isdigit():
            idx = int(token)
            if idx <= 0:
                return ""
            sk = self.session_key_fn(update)
            arr = self._get_thread_index_map(sk, node_id)
            if not arr:
                # Fallback: fetch latest thread list on-demand so users can run `/thread resume 2`
                # directly without a prior `/thread list`.
                core = context.application.bot_data.get("core")
                if core is not None:
                    try:
                        rep = await core.appserver_call(node_id, "thread/list", {"limit": 20}, timeout_s=min(60.0, self.task_timeout_s))
                        if bool(rep.get("ok")):
                            result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
                            data = result.get("data") if isinstance(result.get("data"), list) else []
                            arr = []
                            for item in data[:20]:
                                if isinstance(item, dict):
                                    tid = str(item.get("id") or "").strip()
                                    if tid:
                                        arr.append(tid)
                            if arr:
                                self._set_thread_index_map(sk, node_id, arr)
                                self.save_sessions_fn(self.sessions_ref)
                    except Exception:
                        pass
            if idx <= len(arr):
                return str(arr[idx - 1])
            msg = update.effective_message
            if msg is not None:
                await self.tg_call(
                    lambda: msg.reply_text(f"[{node_id}] idx out of range: {idx} (list_count={len(arr)}). 先执行 /thread list"),
                    timeout_s=15.0,
                    what=f"{usage} idx",
                )
            return ""
        return token

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
        lines: list[str] = [f"threadId: {tid or '(none)'}"]
        lines.append(f"default_model(session): {self.get_default_model(sk) or '(none)'}")
        lines.append(f"default_effort(session): {self.get_default_effort(sk) or '(none)'}")
        core = context.application.bot_data.get("core")
        node_cfg: dict[str, Any] = {}
        if core is not None:
            try:
                cfg_rep = await core.appserver_call(node_id, "config/read", {}, timeout_s=min(60.0, self.task_timeout_s))
                if bool(cfg_rep.get("ok")):
                    cfg_result = cfg_rep.get("result") if isinstance(cfg_rep.get("result"), dict) else {}
                    node_cfg = cfg_result.get("config") if isinstance(cfg_result.get("config"), dict) else {}
            except Exception:
                node_cfg = {}
        lines.append(f"default_model(node): {str(node_cfg.get('model') or '').strip() or '(unknown)'}")
        lines.append(f"default_sandbox(node): {str(node_cfg.get('sandbox_mode') or '').strip() or '(unknown)'}")
        lines.append(f"default_approval(node): {str(node_cfg.get('approval_policy') or '').strip() or '(unknown)'}")
        if tid:
            if core is not None:
                try:
                    rep = await core.appserver_call(
                        node_id,
                        "thread/read",
                        {"threadId": tid, "includeTurns": False},
                        timeout_s=min(60.0, self.task_timeout_s),
                    )
                    if bool(rep.get("ok")):
                        result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
                        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
                        cwd = str(thread.get("cwd") or "").strip()
                        sandbox = str(
                            thread.get("sandbox")
                            or thread.get("sandboxMode")
                            or thread.get("sandbox_mode")
                            or node_cfg.get("sandbox_mode")
                            or ""
                        ).strip()
                        approval = str(
                            thread.get("approvalPolicy")
                            or thread.get("approval_policy")
                            or node_cfg.get("approval_policy")
                            or ""
                        ).strip()
                        model = str(
                            thread.get("model")
                            or thread.get("model_id")
                            or node_cfg.get("model")
                            or ""
                        ).strip()
                        lines.append(f"model: {model or '(unknown)'}")
                        lines.append(f"sandbox: {sandbox or '(unknown)'}")
                        lines.append(f"approval: {approval or '(unknown)'}")
                        lines.append(f"cwd: {cwd or '(unknown)'}")
                except Exception:
                    pass
        await self.tg_call(lambda: msg.reply_text("\n".join(lines)), timeout_s=15.0, what="/thread_current reply")

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
        raw = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        thread_id = await self._resolve_thread_token(update=update, context=context, node_id=node_id, token=raw, usage="/thread_resume")
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
        idx_ids: list[str] = []
        for i, item in enumerate(data[:20], start=1):
            if not isinstance(item, dict):
                continue
            tid = str(item.get("id") or "")
            preview = str(item.get("preview") or "")
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            stype = str(status.get("type") or "")
            lines.append(f"{i}. {tid} [{stype}] {preview[:80]}")
            if tid:
                idx_ids.append(tid)
        sk = self.session_key_fn(update)
        self._set_thread_index_map(sk, node_id, idx_ids)
        self.save_sessions_fn(self.sessions_ref)
        if result.get("nextCursor"):
            lines.append(f"nextCursor: {result.get('nextCursor')}")
        if idx_ids:
            lines.append("")
            lines.append("tips: /thread resume <idx|threadId>, /thread read <idx|threadId>")
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
        raw = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        thread_id = await self._resolve_thread_token(update=update, context=context, node_id=node_id, token=raw, usage="/thread_read")
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
        raw = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        thread_id = await self._resolve_thread_token(update=update, context=context, node_id=node_id, token=raw, usage="/thread_archive")
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
        raw = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        thread_id = await self._resolve_thread_token(update=update, context=context, node_id=node_id, token=raw, usage="/thread_unarchive")
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

    async def cmd_thread_fork(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /thread_fork chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        msg = update.effective_message
        if msg is None:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: msg.reply_text("unauthorized"), timeout_s=15.0, what="/thread_fork reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        kv = self.parse_kv(context.args or [])
        source_token = (kv.get("threadId") or (context.args[0] if context.args else "")).strip()
        sk = self.session_key_fn(update)
        source_tid = ""
        if source_token:
            source_tid = await self._resolve_thread_token(update=update, context=context, node_id=node_id, token=source_token, usage="/thread_fork")
        if not source_tid:
            source_tid = self.get_current_thread_id(sk, node_id)
        if not source_tid:
            await self.tg_call(lambda: msg.reply_text("usage: /thread fork <idx|threadId> cwd=/path  (or set current thread first)"), timeout_s=15.0, what="/thread_fork reply")
            return
        cwd = str(kv.get("cwd") or "").strip()
        wiz = self._get_fork_wizard(sk, node_id)
        wiz["source_thread_id"] = source_tid
        wiz["cwd"] = cwd
        wiz["sandbox"] = str(wiz.get("sandbox") or "workspace-write")
        wiz["approvalPolicy"] = str(wiz.get("approvalPolicy") or "on-request")
        self.save_sessions_fn(self.sessions_ref)
        text = self._render_fork_text(
            node_id=node_id,
            source_tid=source_tid,
            cwd=str(wiz.get("cwd") or ""),
            sandbox=str(wiz.get("sandbox") or "workspace-write"),
            approval=str(wiz.get("approvalPolicy") or "on-request"),
        )
        kb = self._build_fork_keyboard(sandbox=str(wiz.get("sandbox") or "workspace-write"), approval=str(wiz.get("approvalPolicy") or "on-request"))
        await self.tg_call(lambda: msg.reply_text(text, reply_markup=kb), timeout_s=15.0, what="/thread_fork reply")

    async def on_thread_fork_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        msg = update.effective_message
        if msg is None:
            await q.answer("bad message", show_alert=True)
            return
        if not self.is_allowed(update):
            await q.answer("unauthorized", show_alert=True)
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            await q.answer("node offline", show_alert=True)
            return
        sk = self.session_key_fn(update)
        wiz = self._get_fork_wizard(sk, node_id)
        data = str(q.data or "")
        if data.startswith("thread:fork:sandbox:"):
            wiz["sandbox"] = data.split(":", 3)[3].strip() or "workspace-write"
        elif data.startswith("thread:fork:approval:"):
            wiz["approvalPolicy"] = data.split(":", 3)[3].strip() or "on-request"
        elif data == "thread:fork:cancel":
            self._clear_fork_wizard(sk, node_id)
            self.save_sessions_fn(self.sessions_ref)
            await q.answer("cancelled")
            await self.tg_call(lambda: msg.reply_text(f"[{node_id}] fork cancelled"), timeout_s=15.0, what="thread fork cancel")
            return
        elif data == "thread:fork:create":
            source_tid = str(wiz.get("source_thread_id") or "").strip()
            cwd = str(wiz.get("cwd") or "").strip()
            sandbox = str(wiz.get("sandbox") or "workspace-write").strip()
            approval = str(wiz.get("approvalPolicy") or "on-request").strip()
            if not source_tid:
                await q.answer("missing source", show_alert=True)
                return
            if not cwd:
                await q.answer("请先输入 cwd", show_alert=True)
                return
            core = context.application.bot_data.get("core")
            if core is None:
                await q.answer("manager core missing", show_alert=True)
                return
            params: dict[str, Any] = {"threadId": source_tid, "cwd": cwd, "sandbox": sandbox, "approvalPolicy": approval}
            rep = await core.appserver_call(node_id, "thread/fork", params, timeout_s=min(60.0, self.task_timeout_s))
            if not bool(rep.get("ok")):
                await q.answer("fork failed", show_alert=True)
                await self.tg_call(lambda: msg.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="thread fork create")
                return
            result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
            thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
            new_tid = str(thread.get("id") or "").strip()
            if not new_tid:
                await q.answer("fork missing id", show_alert=True)
                return
            self.set_current_thread_id(sk, node_id, new_tid)
            self._clear_fork_wizard(sk, node_id)
            self.save_sessions_fn(self.sessions_ref)
            await q.answer("fork ok")
            await self.tg_call(
                lambda: msg.reply_text(
                    "\n".join(
                        [
                            f"[{node_id}] ok forked",
                            f"from: {source_tid}",
                            f"to:   {new_tid}",
                            f"cwd: {cwd}",
                            f"sandbox: {sandbox}",
                            f"approval: {approval}",
                        ]
                    )
                ),
                timeout_s=15.0,
                what="thread fork create",
            )
            return
        self.save_sessions_fn(self.sessions_ref)
        text = self._render_fork_text(
            node_id=node_id,
            source_tid=str(wiz.get("source_thread_id") or ""),
            cwd=str(wiz.get("cwd") or ""),
            sandbox=str(wiz.get("sandbox") or "workspace-write"),
            approval=str(wiz.get("approvalPolicy") or "on-request"),
        )
        kb = self._build_fork_keyboard(sandbox=str(wiz.get("sandbox") or "workspace-write"), approval=str(wiz.get("approvalPolicy") or "on-request"))
        await self.tg_call(
            lambda: context.bot.edit_message_text(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                text=text,
                reply_markup=kb,
            ),
            timeout_s=15.0,
            what="thread fork update",
        )
        await q.answer("ok")
