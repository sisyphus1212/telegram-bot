from __future__ import annotations

from typing import Any, Callable

from telegram import Update
from telegram.ext import ContextTypes


class ApprovalHandlers:
    def __init__(
        self,
        *,
        is_allowed: Callable[[Update], bool],
        tg_call: Callable[..., Any],
        core: Any,
        logger: Any,
    ) -> None:
        self.is_allowed = is_allowed
        self.tg_call = tg_call
        self.core = core
        self.logger = logger

    async def cmd_approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        self.logger.info(f"cmd /approve chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/approve reply")
            return
        if not context.args:
            await self.tg_call(lambda: update.message.reply_text("usage: /approve <approval_id> | /approve session <approval_id>"), timeout_s=15.0, what="/approve reply")
            return
        decision = "accept"
        approval_id = ""
        if len(context.args) == 1:
            approval_id = str(context.args[0]).strip()
        elif len(context.args) == 2 and str(context.args[0]).strip().lower() in ("session", "s"):
            approval_id = str(context.args[1]).strip()
            decision = "acceptForSession"
        else:
            await self.tg_call(lambda: update.message.reply_text("usage: /approve <approval_id> | /approve session <approval_id>"), timeout_s=15.0, what="/approve reply")
            return
        if not approval_id:
            await self.tg_call(lambda: update.message.reply_text("usage: /approve <approval_id> | /approve session <approval_id>"), timeout_s=15.0, what="/approve reply")
            return
        ok, msg = await self.core.approval_decide(approval_id=approval_id, decision=decision, chat_id=update.effective_chat.id)
        await self.tg_call(lambda: update.message.reply_text(msg if ok else f"error: {msg}"), timeout_s=15.0, what="/approve reply")

    async def cmd_decline(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        self.logger.info(f"cmd /decline chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/decline reply")
            return
        approval_id = str(context.args[0]).strip() if context.args else ""
        if not approval_id:
            await self.tg_call(lambda: update.message.reply_text("usage: /decline <approval_id>"), timeout_s=15.0, what="/decline reply")
            return
        ok, msg = await self.core.approval_decide(approval_id=approval_id, decision="decline", chat_id=update.effective_chat.id)
        await self.tg_call(lambda: update.message.reply_text(msg if ok else f"error: {msg}"), timeout_s=15.0, what="/decline reply")

    async def on_approve_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        await self.tg_call(lambda: q.answer(), timeout_s=10.0, what="approve callback answer")
        if not self.is_allowed(update):
            await self.tg_call(lambda: q.answer("unauthorized", show_alert=True), timeout_s=10.0, what="approve callback unauthorized")
            return
        data = str(q.data or "")
        if not data.startswith("approve:"):
            return
        parts = data.split(":", 2)
        if len(parts) != 3:
            await self.tg_call(lambda: q.answer("bad approve data", show_alert=True), timeout_s=10.0, what="approve callback bad")
            return
        action = parts[1]
        approval_id = parts[2].strip()
        if not approval_id:
            await self.tg_call(lambda: q.answer("missing approval_id", show_alert=True), timeout_s=10.0, what="approve callback missing")
            return
        if action == "accept":
            decision = "accept"
        elif action == "session":
            decision = "acceptForSession"
        elif action == "decline":
            decision = "decline"
        else:
            await self.tg_call(lambda: q.answer("unknown action", show_alert=True), timeout_s=10.0, what="approve callback unknown")
            return
        chat_id = q.message.chat_id if q.message else (update.effective_chat.id if update.effective_chat else 0)
        ok, msg = await self.core.approval_decide(approval_id=approval_id, decision=decision, chat_id=int(chat_id))
        if ok:
            await self.tg_call(lambda: q.answer("ok"), timeout_s=10.0, what="approve callback ok")
            try:
                await self.tg_call(lambda: q.edit_message_reply_markup(reply_markup=None), timeout_s=15.0, what="approve callback edit")
            except Exception:
                pass
        else:
            await self.tg_call(lambda: q.answer(f"error: {msg}", show_alert=True), timeout_s=10.0, what="approve callback err")
