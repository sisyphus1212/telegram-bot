from __future__ import annotations

from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


class NodeHandlers:
    def __init__(
        self,
        *,
        registry: Any,
        is_allowed: Callable[[Update], bool],
        session_key_fn: Callable[[Update], str],
        get_selected_node: Callable[[str], str],
        set_selected_node: Callable[[str, str], None],
        save_sessions_fn: Callable[[dict[str, dict]], None],
        sessions_ref: dict[str, dict],
        tg_call: Callable[..., Any],
        logger: Any,
    ) -> None:
        self.registry = registry
        self.is_allowed = is_allowed
        self.session_key_fn = session_key_fn
        self.get_selected_node = get_selected_node
        self.set_selected_node = set_selected_node
        self.save_sessions_fn = save_sessions_fn
        self.sessions_ref = sessions_ref
        self.tg_call = tg_call
        self.logger = logger

    def render_node_text(self, *, selected: str, online: list[str], allowed: list[str]) -> str:
        lines: list[str] = []
        lines.append(f"current: {selected or '(none)'}")
        lines.append(f"online_count: {len(online)}")
        if online:
            lines.append("online:")
            for nid in online[:20]:
                lines.append(f"- {self.registry.get_node_label(nid)}")
        lines.append("")
        lines.append("点击按钮选择 node：")
        return "\n".join(lines)

    def build_node_keyboard(self, *, online: list[str], selected: str) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for nid in online[:20]:
            display = self.registry.get_node_label(nid)
            label = f"• {display}" if nid == selected else display
            row.append(InlineKeyboardButton(label[:32], callback_data=f"node:set:{nid}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("Refresh", callback_data="node:refresh")])
        return InlineKeyboardMarkup(rows)

    async def cmd_node(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(
            f"cmd /node chat={update.effective_chat.id if update.effective_chat else '?'} "
            f"user={update.effective_user.id if update.effective_user else '?'}"
        )
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/node reply")
            return
        sk = self.session_key_fn(update)
        selected = self.get_selected_node(sk)
        online = self.registry.online_node_ids()
        allowed = self.registry.allowed_node_ids()
        text = self.render_node_text(selected=selected, online=online, allowed=allowed)
        kb = self.build_node_keyboard(online=online, selected=selected)
        await self.tg_call(lambda: update.message.reply_text(text, reply_markup=kb), timeout_s=15.0, what="/node reply")

    async def on_node_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if not self.is_allowed(update):
            await q.answer("unauthorized", show_alert=True)
            return
        data = str(q.data or "")
        if not data.startswith("node:"):
            return
        sk = self.session_key_fn(update)
        if data == "node:refresh":
            pass
        elif data.startswith("node:set:"):
            node_id = data[len("node:set:") :].strip()
            if not node_id:
                await q.answer("bad node id", show_alert=True)
                return
            if not self.registry.is_online(node_id):
                await q.answer(f"node offline: {node_id}", show_alert=True)
                return
            self.set_selected_node(sk, node_id)
            self.save_sessions_fn(self.sessions_ref)
        else:
            await q.answer()
            return

        selected = self.get_selected_node(sk)
        online = self.registry.online_node_ids()
        allowed = self.registry.allowed_node_ids()
        text = self.render_node_text(selected=selected, online=online, allowed=allowed)
        kb = self.build_node_keyboard(online=online, selected=selected)
        await self.tg_call(lambda: q.edit_message_text(text=text, reply_markup=kb), timeout_s=15.0, what="node callback edit")
        await q.answer(f"current={selected or '(none)'}")
