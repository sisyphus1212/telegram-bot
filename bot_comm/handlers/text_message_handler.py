from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


class TextMessageHandler:
    def __init__(
        self,
        *,
        is_allowed: Callable[[Update], bool],
        session_key_fn: Callable[[Update], str],
        get_selected_node: Callable[[str], str],
        get_current_thread_id: Callable[[str, str], str],
        set_current_thread_id: Callable[[str, str, str], None],
        get_default_model: Callable[[str], str],
        get_default_effort: Callable[[str], str],
        get_result_mode: Callable[[str], str],
        tg_call: Callable[..., Any],
        registry_is_online: Callable[[str], bool],
        save_sessions_fn: Callable[[dict[str, dict]], None],
        sessions_ref: dict[str, dict],
        task_context_cls: Any,
        base_dir: Path,
        task_timeout_s: float,
        logger: Any,
    ) -> None:
        self.is_allowed = is_allowed
        self.session_key_fn = session_key_fn
        self.get_selected_node = get_selected_node
        self.get_current_thread_id = get_current_thread_id
        self.set_current_thread_id = set_current_thread_id
        self.get_default_model = get_default_model
        self.get_default_effort = get_default_effort
        self.get_result_mode = get_result_mode
        self.tg_call = tg_call
        self.registry_is_online = registry_is_online
        self.save_sessions_fn = save_sessions_fn
        self.sessions_ref = sessions_ref
        self.task_context_cls = task_context_cls
        self.base_dir = base_dir
        self.task_timeout_s = task_timeout_s
        self.logger = logger

    def _get_fork_wizard(self, sk: str, node_id: str) -> dict[str, Any]:
        sess = self.sessions_ref.get(sk)
        if not isinstance(sess, dict):
            return {}
        by_node = sess.get("by_node") if isinstance(sess.get("by_node"), dict) else {}
        entry = by_node.get(node_id) if isinstance(by_node.get(node_id), dict) else {}
        wiz = entry.get("fork_wizard")
        if isinstance(wiz, dict):
            return wiz
        return {}

    def _build_fork_select_keyboard(self, *, sandbox: str, approval: str) -> InlineKeyboardMarkup:
        sb_vals = [
            ("workspace-write", "workspace"),
            ("read-only", "readonly"),
            ("danger-full-access", "danger"),
        ]
        ap_vals = [
            ("on-request", "onRequest"),
            ("on-failure", "onFailure"),
            ("untrusted", "unlessTrusted"),
            ("never", "never"),
        ]
        row1 = [InlineKeyboardButton(("• " if sandbox == v else "") + lab, callback_data=f"thread:fork:sandbox:{v}") for v, lab in sb_vals]
        row2 = [InlineKeyboardButton(("• " if approval == v else "") + lab, callback_data=f"thread:fork:approval:{v}") for v, lab in ap_vals[:2]]
        row3 = [InlineKeyboardButton(("• " if approval == v else "") + lab, callback_data=f"thread:fork:approval:{v}") for v, lab in ap_vals[2:]]
        row4 = [
            InlineKeyboardButton("Next: Confirm", callback_data="thread:fork:review"),
            InlineKeyboardButton("Cancel", callback_data="thread:fork:cancel"),
        ]
        return InlineKeyboardMarkup([row1, row2, row3, row4])

    def _render_fork_text(self, *, node_id: str, source_tid: str, cwd: str, sandbox: str, approval: str) -> str:
        return "\n".join(
            [
                f"[{node_id}] thread fork",
                f"source: {source_tid}",
                f"cwd: {cwd or '(missing)'}",
                f"sandbox: {sandbox} (please choose)",
                f"approval: {approval} (please choose)",
                "",
                "已接收路径，请继续选择权限后确认创建。",
            ]
        )

    def _looks_like_path_text(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        if t.lower().startswith("cwd="):
            return True
        return t.startswith("/") or t.startswith("./") or t.startswith("../") or t.startswith("~")

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        trace_id = uuid.uuid4().hex
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        text_len = len(update.message.text) if update.message and update.message.text else 0
        self.logger.info(f"op=tg.update trace_id={trace_id} chat_id={chat_id} user_id={user_id} text_len={text_len}")
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="msg reply")
            return
        if not update.message or not isinstance(update.message.text, str):
            return

        sk = self.session_key_fn(update)
        node_id = self.get_selected_node(sk)
        if not node_id:
            await self.tg_call(lambda: update.message.reply_text("请先 /node 选择一台机器"), timeout_s=15.0, what="msg reply")
            return
        if not self.registry_is_online(node_id):
            await self.tg_call(lambda: update.message.reply_text(f"node offline: {node_id} (use /node)"), timeout_s=15.0, what="msg reply")
            return
        raw_text = update.message.text.strip()
        wiz = self._get_fork_wizard(sk, node_id)
        if isinstance(wiz, dict) and str(wiz.get("source_thread_id") or "").strip() and self._looks_like_path_text(raw_text):
            cwd = raw_text
            if raw_text.lower().startswith("cwd="):
                cwd = raw_text.split("=", 1)[1].strip()
            if cwd:
                wiz["cwd"] = cwd
                wiz["sandbox"] = str(wiz.get("sandbox") or "workspace-write")
                wiz["approvalPolicy"] = str(wiz.get("approvalPolicy") or "on-request")
                wiz["sandbox_selected"] = False
                wiz["approval_selected"] = False
                self.save_sessions_fn(self.sessions_ref)
                text = self._render_fork_text(
                    node_id=node_id,
                    source_tid=str(wiz.get("source_thread_id") or ""),
                    cwd=str(wiz.get("cwd") or ""),
                    sandbox=str(wiz.get("sandbox") or "workspace-write"),
                    approval=str(wiz.get("approvalPolicy") or "on-request"),
                )
                kb = self._build_fork_select_keyboard(
                    sandbox=str(wiz.get("sandbox") or "workspace-write"),
                    approval=str(wiz.get("approvalPolicy") or "on-request"),
                )
                await self.tg_call(lambda: update.message.reply_text(text, reply_markup=kb), timeout_s=15.0, what="fork cwd input")
                return

        task_id = uuid.uuid4().hex
        thread_id = self.get_current_thread_id(sk, node_id)
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="msg reply")
            return
        if not thread_id:
            params: dict[str, Any] = {"cwd": str(self.base_dir), "personality": "pragmatic"}
            default_model = self.get_default_model(sk)
            if default_model:
                params["model"] = default_model
            rep = await core.appserver_call(node_id, "thread/start", params, timeout_s=min(60.0, self.task_timeout_s))
            if not bool(rep.get("ok")):
                await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: thread/start failed: {rep.get('error')}"), timeout_s=15.0, what="msg reply")
                return
            result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
            thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: thread/start missing id"), timeout_s=15.0, what="msg reply")
                return
            self.set_current_thread_id(sk, node_id, thread_id)
            self.save_sessions_fn(self.sessions_ref)

        prompt = update.message.text
        placeholder = await self.tg_call(
            lambda: update.message.reply_text(f"working (node={node_id}, threadId={thread_id[-8:]}) ..."),
            timeout_s=15.0,
            what="placeholder",
        )
        self.logger.info(f"op=tg.send ok=true trace_id={trace_id} chat_id={chat_id} msg_id={placeholder.message_id} kind=placeholder node_id={node_id}")

        task_msg: dict[str, Any] = {
            "type": "task_assign",
            "trace_id": trace_id,
            "task_id": task_id,
            "thread_key": sk,
            "thread_id": thread_id,
            "prompt": prompt,
        }
        # Avoid accidental 120s default timeout on node side for long-running turns.
        turn_timeout_s = max(600.0, float(self.task_timeout_s))
        task_msg["timeout_s"] = turn_timeout_s
        session_model = self.get_default_model(sk)
        if session_model:
            task_msg["model"] = session_model
        task_msg["effort"] = self.get_default_effort(sk)

        if chat_id is None:
            return
        await core.register_task(
            self.task_context_cls(
                task_id=task_id,
                trace_id=trace_id,
                node_id=node_id,
                thread_id=thread_id,
                chat_id=int(chat_id),
                placeholder_msg_id=int(placeholder.message_id),
                created_at=time.time(),
                timeout_s=turn_timeout_s,
                session_key=sk,
                result_mode=self.get_result_mode(sk),
            )
        )
        self.logger.info(
            f"op=dispatch.enqueue trace_id={trace_id} node_id={node_id} task_id={task_id} thread_id={thread_id[-8:]} "
            f"chat_id={chat_id} msg_id={placeholder.message_id} prompt_len={len(prompt)}"
        )
        asyncio.create_task(core.send_task_assign(node_id=node_id, task_msg=task_msg), name=f"send:{node_id}:{task_id}")
