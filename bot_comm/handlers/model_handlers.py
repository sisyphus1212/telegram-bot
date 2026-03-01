from __future__ import annotations

from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


class ModelHandlers:
    def __init__(
        self,
        *,
        registry: Any,
        is_allowed: Callable[[Update], bool],
        session_key_fn: Callable[[Update], str],
        get_selected_node: Callable[[str], str],
        get_default_model: Callable[[str], str],
        set_default_model: Callable[[str, str], None],
        get_default_effort: Callable[[str], str],
        set_default_effort: Callable[[str, str], None],
        save_sessions_fn: Callable[[dict[str, dict]], None],
        sessions_ref: dict[str, dict],
        require_node_online: Callable[[Update], Any],
        tg_call: Callable[..., Any],
        task_timeout_s: float,
        logger: Any,
    ) -> None:
        self.registry = registry
        self.is_allowed = is_allowed
        self.session_key_fn = session_key_fn
        self.get_selected_node = get_selected_node
        self.get_default_model = get_default_model
        self.set_default_model = set_default_model
        self.get_default_effort = get_default_effort
        self.set_default_effort = set_default_effort
        self.save_sessions_fn = save_sessions_fn
        self.sessions_ref = sessions_ref
        self.require_node_online = require_node_online
        self.tg_call = tg_call
        self.task_timeout_s = task_timeout_s
        self.logger = logger

    async def _fetch_model_state(self, *, node_id: str, core: Any, session_model: str) -> tuple[str, str, list[str]]:
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

    async def _fetch_model_detail(self, *, node_id: str, core: Any, model_id: str) -> dict[str, Any]:
        rep = await core.appserver_call(node_id, "model/list", {}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            raise RuntimeError(str(rep.get("error") or "model/list failed"))
        result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
        data = result.get("data") if isinstance(result.get("data"), list) else []
        for item in data:
            if isinstance(item, dict) and str(item.get("id") or "").strip() == model_id:
                return item
        return {}

    def _render_model_text(self, *, node_id: str, effective_model: str, proxy_default_model: str, session_model: str, models: list[str], effort: str, model_detail: dict[str, Any] | None) -> str:
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
        if models:
            shown = min(len(models), 12)
            lines.append(f"available: {shown} shown in buttons (total={len(models)})")
        else:
            lines.append("available: (none)")
        return "\n".join(lines)

    def _build_model_keyboard(self, *, models: list[str], effective_model: str, effort: str) -> InlineKeyboardMarkup:
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
        eff_row: list[InlineKeyboardButton] = []
        for e in ("low", "medium", "high"):
            lab = f"• {e}" if e == effort else e
            eff_row.append(InlineKeyboardButton(lab, callback_data=f"effort:set:{e}"))
        rows.append(eff_row)
        rows.append([InlineKeyboardButton("Clear", callback_data="model:clear"), InlineKeyboardButton("Refresh", callback_data="model:refresh")])
        return InlineKeyboardMarkup(rows)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /model chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/model reply")
            return
        sk = self.session_key_fn(update)
        session_model = self.get_default_model(sk)
        session_effort = self.get_default_effort(sk)
        if not context.args:
            node_id = await self.require_node_online(update)
            if not node_id:
                return
            core = context.application.bot_data.get("core")
            if core is None:
                await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/model reply")
                return
            try:
                effective_model, proxy_default_model, models = await self._fetch_model_state(node_id=node_id, core=core, session_model=session_model)
                detail = await self._fetch_model_detail(node_id=node_id, core=core, model_id=effective_model)
            except Exception as e:
                await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: {type(e).__name__}: {e}"), timeout_s=15.0, what="/model reply")
                return
            text = self._render_model_text(node_id=node_id, effective_model=effective_model, proxy_default_model=proxy_default_model, session_model=session_model, models=models, effort=session_effort, model_detail=detail)
            kb = self._build_model_keyboard(models=models, effective_model=effective_model, effort=session_effort)
            await self.tg_call(lambda: update.message.reply_text(text, reply_markup=kb), timeout_s=15.0, what="/model reply")
            return

        arg = " ".join(context.args).strip()
        if context.args and str(context.args[0]).strip().lower() == "effort":
            if len(context.args) < 2:
                await self.tg_call(lambda: update.message.reply_text("usage: /model effort <low|medium|high>"), timeout_s=15.0, what="/model reply")
                return
            eff = str(context.args[1]).strip().lower()
            if eff not in ("low", "medium", "high"):
                await self.tg_call(lambda: update.message.reply_text("usage: /model effort <low|medium|high>"), timeout_s=15.0, what="/model reply")
                return
            self.set_default_effort(sk, eff)
            self.save_sessions_fn(self.sessions_ref)
            await self.tg_call(lambda: update.message.reply_text(f"ok effort={eff}"), timeout_s=15.0, what="/model reply")
            return
        if arg.lower() in ("clear", "default", "reset"):
            self.set_default_model(sk, "")
            self.save_sessions_fn(self.sessions_ref)
            await self.tg_call(lambda: update.message.reply_text("ok model=(default)"), timeout_s=15.0, what="/model reply")
            return
        self.set_default_model(sk, arg)
        self.save_sessions_fn(self.sessions_ref)
        await self.tg_call(lambda: update.message.reply_text(f"ok model={arg}"), timeout_s=15.0, what="/model reply")

    async def on_model_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if not self.is_allowed(update):
            await q.answer("unauthorized", show_alert=True)
            return
        data = str(q.data or "")
        if not data.startswith("model:"):
            return
        sk = self.session_key_fn(update)
        node_id = self.get_selected_node(sk)
        if not node_id or not self.registry.is_online(node_id):
            await q.answer("node offline", show_alert=True)
            return
        core = context.application.bot_data.get("core")
        if core is None:
            await q.answer("manager core missing", show_alert=True)
            return
        if data == "model:clear":
            self.set_default_model(sk, "")
            self.save_sessions_fn(self.sessions_ref)
        elif data == "model:refresh":
            pass
        elif data.startswith("model:set:"):
            model_id = data[len("model:set:") :].strip()
            if not model_id:
                await q.answer("bad model id", show_alert=True)
                return
            self.set_default_model(sk, model_id)
            self.save_sessions_fn(self.sessions_ref)
        else:
            await q.answer()
            return

        session_model = self.get_default_model(sk)
        session_effort = self.get_default_effort(sk)
        try:
            effective_model, proxy_default_model, models = await self._fetch_model_state(node_id=node_id, core=core, session_model=session_model)
            detail = await self._fetch_model_detail(node_id=node_id, core=core, model_id=effective_model)
        except Exception as e:
            await q.answer(f"{type(e).__name__}: {e}", show_alert=True)
            return
        text = self._render_model_text(node_id=node_id, effective_model=effective_model, proxy_default_model=proxy_default_model, session_model=session_model, models=models, effort=session_effort, model_detail=detail)
        kb = self._build_model_keyboard(models=models, effective_model=effective_model, effort=session_effort)
        await self.tg_call(lambda: q.edit_message_text(text=text, reply_markup=kb), timeout_s=15.0, what="model callback edit")
        await q.answer(f"model={session_model or proxy_default_model or '(default)'}")

    async def on_effort_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if not self.is_allowed(update):
            await q.answer("unauthorized", show_alert=True)
            return
        data = str(q.data or "")
        if not data.startswith("effort:"):
            return
        sk = self.session_key_fn(update)
        node_id = self.get_selected_node(sk)
        if not node_id or not self.registry.is_online(node_id):
            await q.answer("node offline", show_alert=True)
            return
        core = context.application.bot_data.get("core")
        if core is None:
            await q.answer("manager core missing", show_alert=True)
            return
        if data.startswith("effort:set:"):
            eff = data[len("effort:set:") :].strip().lower()
            if eff not in ("low", "medium", "high"):
                await q.answer("bad effort", show_alert=True)
                return
            self.set_default_effort(sk, eff)
            self.save_sessions_fn(self.sessions_ref)
        else:
            await q.answer()
            return
        session_model = self.get_default_model(sk)
        session_effort = self.get_default_effort(sk)
        try:
            effective_model, proxy_default_model, models = await self._fetch_model_state(node_id=node_id, core=core, session_model=session_model)
            detail = await self._fetch_model_detail(node_id=node_id, core=core, model_id=effective_model)
        except Exception as e:
            await q.answer(f"{type(e).__name__}: {e}", show_alert=True)
            return
        text = self._render_model_text(node_id=node_id, effective_model=effective_model, proxy_default_model=proxy_default_model, session_model=session_model, models=models, effort=session_effort, model_detail=detail)
        kb = self._build_model_keyboard(models=models, effective_model=effective_model, effort=session_effort)
        await self.tg_call(lambda: q.edit_message_text(text=text, reply_markup=kb), timeout_s=15.0, what="effort callback edit")
        await q.answer(f"effort={session_effort}")
