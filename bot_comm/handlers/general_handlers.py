from __future__ import annotations

from typing import Any, Callable

from telegram import Update
from telegram.ext import ContextTypes


class GeneralHandlers:
    def __init__(
        self,
        *,
        is_allowed: Callable[[Update], bool],
        tg_call: Callable[..., Any],
        session_key_fn: Callable[[Update], str],
        get_result_mode: Callable[[str], str],
        set_result_mode: Callable[[str, str], None],
        save_sessions_fn: Callable[[dict[str, dict]], None],
        sessions_ref: dict[str, dict],
        logger: Any,
    ) -> None:
        self.is_allowed = is_allowed
        self.tg_call = tg_call
        self.session_key_fn = session_key_fn
        self.get_result_mode = get_result_mode
        self.set_result_mode = set_result_mode
        self.save_sessions_fn = save_sessions_fn
        self.sessions_ref = sessions_ref
        self.logger = logger

    async def cmd_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(
            f"cmd /ping from chat={update.effective_chat.id if update.effective_chat else '?'} "
            f"user={update.effective_user.id if update.effective_user else '?'}"
        )
        if not update.effective_chat:
            return
        core = context.application.bot_data.get("core")
        if not self.is_allowed(update):
            if core is not None and await core.tg_send(chat_id=update.effective_chat.id, text="unauthorized", kind="ping", retries_left=12):
                return
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/ping reply")
            return
        if core is not None and await core.tg_send(chat_id=update.effective_chat.id, text="pong", kind="ping", timeout_s=5.0, retries_left=12):
            return
        await self.tg_call(lambda: update.message.reply_text("pong"), timeout_s=15.0, what="/ping reply")

    async def cmd_result(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /result chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/result reply")
            return
        sk = self.session_key_fn(update)
        if not context.args:
            mode = self.get_result_mode(sk)
            await self.tg_call(lambda: update.message.reply_text(f"result: {mode}"), timeout_s=15.0, what="/result reply")
            return
        mode = (context.args[0] or "").strip().lower()
        aliases = {"replace": "replace", "edit": "replace", "send": "send", "separate": "send"}
        mode = aliases.get(mode, "")
        if not mode:
            await self.tg_call(lambda: update.message.reply_text("usage: /result <replace|send>"), timeout_s=15.0, what="/result reply")
            return
        self.set_result_mode(sk, mode)
        self.save_sessions_fn(self.sessions_ref)
        await self.tg_call(lambda: update.message.reply_text(f"ok result={mode}"), timeout_s=15.0, what="/result reply")
