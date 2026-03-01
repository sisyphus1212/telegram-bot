from __future__ import annotations

import json
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

    def _get_nodecap_wizard(self, sk: str, node_id: str) -> dict[str, Any]:
        sess = self.sessions_ref.setdefault(sk, {"node": node_id, "by_node": {}, "defaults": {}})
        by_node = sess.setdefault("by_node", {}) if isinstance(sess, dict) else {}
        entry = by_node.setdefault(node_id, {}) if isinstance(by_node, dict) else {}
        wiz = entry.get("nodecap_wizard")
        if not isinstance(wiz, dict):
            wiz = {}
            entry["nodecap_wizard"] = wiz
        return wiz

    def _clear_nodecap_wizard(self, sk: str, node_id: str) -> None:
        sess = self.sessions_ref.get(sk)
        if not isinstance(sess, dict):
            return
        by_node = sess.get("by_node") if isinstance(sess.get("by_node"), dict) else {}
        entry = by_node.get(node_id) if isinstance(by_node.get(node_id), dict) else {}
        entry.pop("nodecap_wizard", None)

    def _template_caps(self, key: str) -> Any:
        k = (key or "").strip().lower()
        if k == "devops":
            return {"roles": ["dev", "ops"], "note": "interactive workstation"}
        if k == "build":
            return {"roles": ["build"], "note": "ci/build host"}
        if k == "ops":
            return {"roles": ["ops"], "note": "ops/ansible host"}
        if k == "empty":
            return {}
        return None

    def _nodecap_keyboard(self, selected_template: str) -> InlineKeyboardMarkup:
        row1 = [
            InlineKeyboardButton(("• " if selected_template == "devops" else "") + "DevOps", callback_data="nodecap:tpl:devops"),
            InlineKeyboardButton(("• " if selected_template == "build" else "") + "Build", callback_data="nodecap:tpl:build"),
            InlineKeyboardButton(("• " if selected_template == "ops" else "") + "Ops", callback_data="nodecap:tpl:ops"),
        ]
        row2 = [
            InlineKeyboardButton(("• " if selected_template == "empty" else "") + "Clear", callback_data="nodecap:tpl:empty"),
        ]
        row3 = [
            InlineKeyboardButton("Next: Confirm", callback_data="nodecap:review"),
            InlineKeyboardButton("Cancel", callback_data="nodecap:cancel"),
        ]
        return InlineKeyboardMarkup([row1, row2, row3])

    def _nodecap_confirm_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Apply", callback_data="nodecap:apply"),
                InlineKeyboardButton("Back", callback_data="nodecap:back"),
                InlineKeyboardButton("Cancel", callback_data="nodecap:cancel"),
            ]]
        )

    def _render_nodecap_text(self, *, node_id: str, current_caps: str, selected_template: str, pending_caps: str) -> str:
        return "\n".join(
            [
                f"[{node_id}] node.meta.set",
                f"current_capabilities: {current_caps}",
                f"selected_template: {selected_template or '(none)'}",
                "",
                "pending_capabilities:",
                pending_caps,
                "",
                "先选模板，再点 Next: Confirm",
            ]
        )

    def _render_nodecap_confirm_text(self, *, node_id: str, pending_caps: str) -> str:
        return "\n".join(
            [
                f"[{node_id}] node.meta.set confirm",
                "action: node.meta.set",
                "",
                "payload.capabilities:",
                pending_caps,
            ]
        )

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

    def _format_capabilities(self, node_id: str) -> str:
        caps = self.registry.get_node_capabilities(node_id)
        if caps is None or caps == "":
            return "(none)"
        if isinstance(caps, str):
            return caps
        try:
            return json.dumps(caps, ensure_ascii=False, indent=2)
        except Exception:
            return str(caps)

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

    async def cmd_nodecap(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: msg.reply_text("unauthorized"), timeout_s=15.0, what="/nodecap reply")
            return
        sk = self.session_key_fn(update)
        node_id = self.get_selected_node(sk)
        if not node_id:
            await self.tg_call(lambda: msg.reply_text("请先 /node 选择一台机器"), timeout_s=15.0, what="/nodecap reply")
            return
        if not self.registry.is_online(node_id):
            await self.tg_call(lambda: msg.reply_text(f"node offline: {node_id} (use /node)"), timeout_s=15.0, what="/nodecap reply")
            return
        wiz = self._get_nodecap_wizard(sk, node_id)
        if "template" not in wiz:
            wiz["template"] = ""
        if "capabilities" not in wiz:
            wiz["capabilities"] = self.registry.get_node_capabilities(node_id)
        self.save_sessions_fn(self.sessions_ref)
        current_caps = self._format_capabilities(node_id)
        pending_caps = self._format_capabilities(node_id) if wiz.get("capabilities") is None else (
            json.dumps(wiz.get("capabilities"), ensure_ascii=False, indent=2)
            if isinstance(wiz.get("capabilities"), (dict, list))
            else str(wiz.get("capabilities"))
        )
        text = self._render_nodecap_text(
            node_id=node_id,
            current_caps=current_caps,
            selected_template=str(wiz.get("template") or ""),
            pending_caps=pending_caps,
        )
        kb = self._nodecap_keyboard(str(wiz.get("template") or ""))
        await self.tg_call(lambda: msg.reply_text(text, reply_markup=kb), timeout_s=15.0, what="/nodecap reply")

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
            if q.message is not None:
                caps_text = self._format_capabilities(node_id)
                await self.tg_call(
                    lambda: q.message.reply_text(f"node selected: {self.registry.get_node_label(node_id)}\ncapabilities:\n{caps_text}"),
                    timeout_s=15.0,
                    what="node capabilities reply",
                )
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

    async def on_nodecap_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if not self.is_allowed(update):
            await q.answer("unauthorized", show_alert=True)
            return
        msg = update.effective_message
        if msg is None:
            return
        sk = self.session_key_fn(update)
        node_id = self.get_selected_node(sk)
        if not node_id:
            await q.answer("请先 /node 选择机器", show_alert=True)
            return
        if not self.registry.is_online(node_id):
            await q.answer(f"node offline: {node_id}", show_alert=True)
            return
        wiz = self._get_nodecap_wizard(sk, node_id)
        data = str(q.data or "")
        if data.startswith("nodecap:tpl:"):
            tpl = data.split(":", 2)[2].strip()
            caps = self._template_caps(tpl)
            if caps is None:
                await q.answer("unknown template", show_alert=True)
                return
            wiz["template"] = tpl
            wiz["capabilities"] = caps
            self.save_sessions_fn(self.sessions_ref)
        elif data == "nodecap:cancel":
            self._clear_nodecap_wizard(sk, node_id)
            self.save_sessions_fn(self.sessions_ref)
            await q.answer("cancelled")
            return
        elif data == "nodecap:review":
            if not str(wiz.get("template") or "").strip():
                await q.answer("请先选择模板", show_alert=True)
                return
            caps = wiz.get("capabilities")
            caps_text = json.dumps(caps, ensure_ascii=False, indent=2) if isinstance(caps, (dict, list)) else str(caps)
            text = self._render_nodecap_confirm_text(node_id=node_id, pending_caps=caps_text)
            await self.tg_call(
                lambda: context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text=text, reply_markup=self._nodecap_confirm_keyboard()),
                timeout_s=15.0,
                what="nodecap review",
            )
            await q.answer("confirm")
            return
        elif data == "nodecap:back":
            pass
        elif data == "nodecap:apply":
            caps = wiz.get("capabilities")
            ok = await self.registry.set_node_capabilities(node_id=node_id, capabilities=caps)
            if not ok:
                await q.answer("node offline", show_alert=True)
                return
            self._clear_nodecap_wizard(sk, node_id)
            self.save_sessions_fn(self.sessions_ref)
            await q.answer("updated")
            caps_text = self._format_capabilities(node_id)
            await self.tg_call(lambda: msg.reply_text(f"[{node_id}] capabilities updated:\n{caps_text}"), timeout_s=15.0, what="nodecap apply")
            return
        else:
            await q.answer()
            return

        current_caps = self._format_capabilities(node_id)
        pending = wiz.get("capabilities")
        pending_caps = json.dumps(pending, ensure_ascii=False, indent=2) if isinstance(pending, (dict, list)) else str(pending)
        text = self._render_nodecap_text(
            node_id=node_id,
            current_caps=current_caps,
            selected_template=str(wiz.get("template") or ""),
            pending_caps=pending_caps,
        )
        kb = self._nodecap_keyboard(str(wiz.get("template") or ""))
        await self.tg_call(
            lambda: context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text=text, reply_markup=kb),
            timeout_s=15.0,
            what="nodecap update",
        )
        await q.answer("ok")
