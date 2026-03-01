from __future__ import annotations

from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


class ThreadHandlers:
    def __init__(
        self,
        *,
        is_allowed: Callable[[Update], bool],
        tg_call: Callable[..., Any],
        cmd_thread_current: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        cmd_thread_start: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        cmd_thread_resume: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        cmd_thread_list: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        cmd_thread_read: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        cmd_thread_archive: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        cmd_thread_unarchive: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        cmd_thread_fork: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        on_thread_fork_callback: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        logger: Any,
    ) -> None:
        self.is_allowed = is_allowed
        self.tg_call = tg_call
        self.cmd_thread_current = cmd_thread_current
        self.cmd_thread_start = cmd_thread_start
        self.cmd_thread_resume = cmd_thread_resume
        self.cmd_thread_list = cmd_thread_list
        self.cmd_thread_read = cmd_thread_read
        self.cmd_thread_archive = cmd_thread_archive
        self.cmd_thread_unarchive = cmd_thread_unarchive
        self.cmd_thread_fork = cmd_thread_fork
        self.on_thread_fork_callback = on_thread_fork_callback
        self.logger = logger

    async def cmd_thread(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(
            f"cmd /thread chat={update.effective_chat.id if update.effective_chat else '?'} "
            f"user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}"
        )
        msg = update.effective_message
        if msg is None:
            return
        if not context.args:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("/thread start", callback_data="thread:shortcut:start"),
                InlineKeyboardButton("/thread list", callback_data="thread:shortcut:list"),
                InlineKeyboardButton("/thread current", callback_data="thread:shortcut:current"),
            ]])
            await self.tg_call(
                lambda: msg.reply_text(
                    "usage: /thread <current|start|resume|list|read|archive|unarchive|fork> ...\n\n"
                    "快捷按钮：",
                    reply_markup=kb,
                ),
                timeout_s=15.0,
                what="/thread reply",
            )
            return
        sub = str(context.args[0] or "").strip().lower()
        rest = list(context.args[1:])
        orig_args = context.args
        context.args = rest
        try:
            if sub == "current":
                await self.cmd_thread_current(update, context)
            elif sub == "start":
                await self.cmd_thread_start(update, context)
            elif sub == "resume":
                await self.cmd_thread_resume(update, context)
            elif sub == "list":
                await self.cmd_thread_list(update, context)
            elif sub == "read":
                await self.cmd_thread_read(update, context)
            elif sub == "archive":
                await self.cmd_thread_archive(update, context)
            elif sub == "unarchive":
                await self.cmd_thread_unarchive(update, context)
            elif sub == "fork":
                await self.cmd_thread_fork(update, context)
            else:
                await self.tg_call(
                    lambda: msg.reply_text("usage: /thread <current|start|resume|list|read|archive|unarchive|fork> ..."),
                    timeout_s=15.0,
                    what="/thread reply",
                )
        finally:
            context.args = orig_args

    async def on_thread_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if not self.is_allowed(update):
            await q.answer("unauthorized", show_alert=True)
            return
        data = str(q.data or "")
        if not data.startswith("thread:shortcut:"):
            if data.startswith("thread:fork:"):
                await self.on_thread_fork_callback(update, context)
            return
        action = data.split(":", 2)[2].strip()
        orig_args = context.args
        context.args = []
        try:
            if action == "current":
                await self.cmd_thread_current(update, context)
            elif action == "list":
                await self.cmd_thread_list(update, context)
            elif action == "start":
                context.args = [
                    "sandbox=danger-full-access",
                    "approvalPolicy=onRequest",
                    "personality=pragmatic",
                ]
                await self.cmd_thread_start(update, context)
            else:
                await q.answer("bad action", show_alert=True)
                return
        finally:
            context.args = orig_args
        await q.answer("ok")
