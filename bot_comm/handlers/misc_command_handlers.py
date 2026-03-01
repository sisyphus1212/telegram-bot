from __future__ import annotations

from typing import Any, Callable

from telegram import Update
from telegram.ext import ContextTypes


class MiscCommandHandlers:
    def __init__(
        self,
        *,
        tg_call: Callable[..., Any],
        logger: Any,
        cmd_config_read: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        cmd_config_value_write: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        cmd_skills_list: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
        cmd_collaborationmode_list: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
    ) -> None:
        self.tg_call = tg_call
        self.logger = logger
        self.cmd_config_read = cmd_config_read
        self.cmd_config_value_write = cmd_config_value_write
        self.cmd_skills_list = cmd_skills_list
        self.cmd_collaborationmode_list = cmd_collaborationmode_list

    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /config chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not context.args:
            await self.tg_call(lambda: update.message.reply_text("usage: /config read ... | /config write keyPath=<...> value=<json> ..."), timeout_s=15.0, what="/config reply")
            return
        sub = str(context.args[0] or "").strip().lower()
        rest = list(context.args[1:])
        orig_args = context.args
        context.args = rest
        try:
            if sub == "read":
                await self.cmd_config_read(update, context)
            elif sub == "write":
                await self.cmd_config_value_write(update, context)
            else:
                await self.tg_call(lambda: update.message.reply_text("usage: /config read ... | /config write keyPath=<...> value=<json> ..."), timeout_s=15.0, what="/config reply")
        finally:
            context.args = orig_args

    async def cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /skills chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not context.args:
            await self.tg_call(lambda: update.message.reply_text("usage: /skills list [cwds=/a,/b] [forceReload=true|false]"), timeout_s=15.0, what="/skills reply")
            return
        sub = str(context.args[0] or "").strip().lower()
        rest = list(context.args[1:])
        orig_args = context.args
        context.args = rest
        try:
            if sub == "list":
                await self.cmd_skills_list(update, context)
            else:
                await self.tg_call(lambda: update.message.reply_text("usage: /skills list [cwds=/a,/b] [forceReload=true|false]"), timeout_s=15.0, what="/skills reply")
        finally:
            context.args = orig_args

    async def cmd_collaborationmode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /collaborationmode chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not context.args:
            await self.tg_call(lambda: update.message.reply_text("usage: /collaborationmode list"), timeout_s=15.0, what="/collaborationmode reply")
            return
        sub = str(context.args[0] or "").strip().lower()
        rest = list(context.args[1:])
        orig_args = context.args
        context.args = rest
        try:
            if sub == "list":
                await self.cmd_collaborationmode_list(update, context)
            else:
                await self.tg_call(lambda: update.message.reply_text("usage: /collaborationmode list"), timeout_s=15.0, what="/collaborationmode reply")
        finally:
            context.args = orig_args
