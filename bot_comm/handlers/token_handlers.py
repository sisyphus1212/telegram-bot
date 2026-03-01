from __future__ import annotations

import json
from typing import Any, Callable

from telegram import Update
from telegram.ext import ContextTypes


class TokenHandlers:
    def __init__(
        self,
        *,
        node_auth: Any,
        is_allowed: Callable[[Update], bool],
        parse_kv: Callable[[list[str]], dict[str, str]],
        fmt_unix_ts: Callable[[Any], str],
        build_node_config: Callable[[str, str], dict[str, Any]],
        tg_call: Callable[..., Any],
        logger: Any,
    ) -> None:
        self.node_auth = node_auth
        self.is_allowed = is_allowed
        self.parse_kv = parse_kv
        self.fmt_unix_ts = fmt_unix_ts
        self.build_node_config = build_node_config
        self.tg_call = tg_call
        self.logger = logger

    async def cmd_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(
            f"cmd /token chat={update.effective_chat.id if update.effective_chat else '?'} "
            f"user={update.effective_user.id if update.effective_user else '?'} args={context.args!r}"
        )
        msg = update.effective_message
        if msg is None:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: msg.reply_text("unauthorized"), timeout_s=15.0, what="/token reply")
            return
        if not context.args:
            await self.tg_call(lambda: msg.reply_text("usage: /token <generate|list|revoke> ..."), timeout_s=15.0, what="/token reply")
            return

        sub = str(context.args[0] or "").strip().lower()
        args = list(context.args[1:])
        # Shortcut: /token <node_id> => generate a token for this node_id.
        if sub not in ("generate", "list", "revoke"):
            args = [f"node_id={sub}", *args]
            sub = "generate"
        kv = self.parse_kv(args)
        uid = update.effective_user.id if update.effective_user else None

        if sub == "generate":
            node_id = str(kv.get("node") or kv.get("node_id") or (args[0] if args and "=" not in str(args[0]) else "")).strip()
            note = str(kv.get("note") or "").strip()
            if not node_id:
                await self.tg_call(
                    lambda: msg.reply_text("usage: /token generate node_id=<node_id> [note=<text>]"),
                    timeout_s=15.0,
                    what="/token generate",
                )
                return
            rec = await self.node_auth.generate(node_id=node_id, note=note, created_by=uid if isinstance(uid, int) else None)
            token = str(rec.get("token") or "")
            token_id = str(rec.get("token_id") or "")
            node_cfg = self.build_node_config(node_id, token)
            lines = [
                "token created",
                f"token_id: {token_id}",
                f"node_id: {node_id}",
                f"note: {note or '(none)'}",
                "",
                "token (save now, manager 不会再次明文展示):",
                token,
            ]
            self.logger.info(f"token generated token_id={token_id} node_id={node_id}")
            await self.tg_call(lambda: msg.reply_text("\n".join(lines)), timeout_s=15.0, what="/token generate")
            cfg_text = "node_config.json (copy as full file):\n" + json.dumps(node_cfg, ensure_ascii=False, indent=2)
            await self.tg_call(lambda: msg.reply_text(cfg_text), timeout_s=15.0, what="/token generate config")
            local_note = (
                "本地接口注释（不要放进 JSON 文件）:\n"
                "// manager_ws local debug: ws://127.0.0.1:8765"
            )
            await self.tg_call(lambda: msg.reply_text(local_note), timeout_s=15.0, what="/token generate local note")
            return

        if sub == "list":
            include_revoked = str(kv.get("revoked") or "false").strip().lower() in ("1", "true", "yes", "y", "on")
            items = await self.node_auth.list_items(include_revoked=include_revoked)
            lines = [f"tokens: {len(items)} (revoked={'yes' if include_revoked else 'no'})"]
            for i, it in enumerate(items[:50], start=1):
                token_id = str(it.get("token_id") or "")
                node_id = str(it.get("node_id") or "") or "(any)"
                note = str(it.get("note") or "") or "-"
                created_at = self.fmt_unix_ts(it.get("created_at"))
                revoked_at = self.fmt_unix_ts(it.get("revoked_at")) if it.get("revoked_at") else "-"
                lines.append(f"{i}. {token_id} node={node_id} created={created_at} revoked={revoked_at} note={note}")
            if len(items) > 50:
                lines.append(f"... truncated, total={len(items)}")
            await self.tg_call(lambda: msg.reply_text("\n".join(lines)), timeout_s=15.0, what="/token list")
            return

        if sub == "revoke":
            token_id = str(kv.get("id") or kv.get("token_id") or (args[0] if args else "")).strip()
            if not token_id:
                await self.tg_call(lambda: msg.reply_text("usage: /token revoke <token_id>"), timeout_s=15.0, what="/token revoke")
                return
            ok, reason = await self.node_auth.revoke(token_id=token_id, revoked_by=uid if isinstance(uid, int) else None)
            if ok:
                await self.tg_call(lambda: msg.reply_text(f"ok revoked token_id={token_id}"), timeout_s=15.0, what="/token revoke")
            else:
                await self.tg_call(lambda: msg.reply_text(f"error: {reason}"), timeout_s=15.0, what="/token revoke")
            return

        await self.tg_call(lambda: msg.reply_text("usage: /token <generate|list|revoke> ..."), timeout_s=15.0, what="/token reply")
