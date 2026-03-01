from __future__ import annotations

import json
from typing import Any, Callable

from telegram import Update
from telegram.ext import ContextTypes


class RemoteCommandHandlers:
    def __init__(
        self,
        *,
        is_allowed: Callable[[Update], bool],
        tg_call: Callable[..., Any],
        parse_kv: Callable[[list[str]], dict[str, str]],
        require_node_online: Callable[[Update], Any],
        pretty_json: Callable[[Any], str],
        task_timeout_s: float,
        logger: Any,
    ) -> None:
        self.is_allowed = is_allowed
        self.tg_call = tg_call
        self.parse_kv = parse_kv
        self.require_node_online = require_node_online
        self.pretty_json = pretty_json
        self.task_timeout_s = task_timeout_s
        self.logger = logger

    async def cmd_skills_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /skills_list chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/skills_list reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        kv = self.parse_kv(context.args or [])
        params: dict[str, Any] = {}
        if "cwds" in kv:
            params["cwds"] = [x for x in str(kv["cwds"]).split(",") if x]
        if "forceReload" in kv:
            params["forceReload"] = str(kv["forceReload"]).lower() in ("1", "true", "yes", "y", "on")
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/skills_list reply")
            return
        rep = await core.appserver_call(node_id, "skills/list", params, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/skills_list reply")
            return
        await self.tg_call(lambda: update.message.reply_text(self.pretty_json(rep.get("result"))), timeout_s=15.0, what="/skills_list reply")

    async def cmd_config_read(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /config_read chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/config_read reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        kv = self.parse_kv(context.args or [])
        params: dict[str, Any] = {}
        if "includeLayers" in kv:
            params["includeLayers"] = str(kv["includeLayers"]).lower() in ("1", "true", "yes", "y", "on")
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/config_read reply")
            return
        rep = await core.appserver_call(node_id, "config/read", params, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/config_read reply")
            return
        await self.tg_call(lambda: update.message.reply_text(self.pretty_json(rep.get("result"))), timeout_s=15.0, what="/config_read reply")

    async def cmd_config_value_write(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /config_value_write chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/config_value_write reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        kv = self.parse_kv(context.args or [])
        key_path = (kv.get("keyPath") or "").strip()
        if not key_path or "value" not in kv:
            await self.tg_call(lambda: update.message.reply_text("usage: /config_value_write keyPath=<...> value=<json> [mergeStrategy=replace|upsert]"), timeout_s=15.0, what="/config_value_write reply")
            return
        try:
            value_obj = json.loads(kv["value"])
        except Exception as e:
            await self.tg_call(lambda: update.message.reply_text(f"bad value json: {type(e).__name__}: {e}"), timeout_s=15.0, what="/config_value_write reply")
            return
        params: dict[str, Any] = {"keyPath": key_path, "value": value_obj}
        if "mergeStrategy" in kv:
            params["mergeStrategy"] = kv["mergeStrategy"]
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/config_value_write reply")
            return
        rep = await core.appserver_call(node_id, "config/value/write", params, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/config_value_write reply")
            return
        await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] ok"), timeout_s=15.0, what="/config_value_write reply")

    async def cmd_collaborationmode_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /collaborationmode_list chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/collaborationmode_list reply")
            return
        node_id = await self.require_node_online(update)
        if not node_id:
            return
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/collaborationmode_list reply")
            return
        rep = await core.appserver_call(node_id, "collaborationMode/list", {}, timeout_s=min(60.0, self.task_timeout_s))
        if not bool(rep.get("ok")):
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: {rep.get('error')}"), timeout_s=15.0, what="/collaborationmode_list reply")
            return
        await self.tg_call(lambda: update.message.reply_text(self.pretty_json(rep.get("result"))), timeout_s=15.0, what="/collaborationmode_list reply")
