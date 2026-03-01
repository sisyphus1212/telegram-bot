from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable

from telegram import Update
from telegram.ext import ContextTypes


class StatusHandlers:
    def __init__(
        self,
        *,
        base_dir: Any,
        registry: Any,
        is_allowed: Callable[[Update], bool],
        session_key_fn: Callable[[Update], str],
        get_selected_node: Callable[[str], str],
        get_default_model: Callable[[str], str],
        get_current_thread_id: Callable[[str, str], str],
        get_result_mode: Callable[[str], str],
        tg_call: Callable[..., Any],
        task_timeout_s: float,
        logger: Any,
    ) -> None:
        self.base_dir = base_dir
        self.registry = registry
        self.is_allowed = is_allowed
        self.session_key_fn = session_key_fn
        self.get_selected_node = get_selected_node
        self.get_default_model = get_default_model
        self.get_current_thread_id = get_current_thread_id
        self.get_result_mode = get_result_mode
        self.tg_call = tg_call
        self.task_timeout_s = task_timeout_s
        self.logger = logger

    def _fmt_unix_ts(self, value: Any) -> str:
        try:
            ts = float(value)
        except Exception:
            return "(unknown)"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _append_rate_limit_lines(self, lines: list[str], rate_result: dict[str, Any]) -> None:
        rl_map = rate_result.get("rateLimitsByLimitId") if isinstance(rate_result.get("rateLimitsByLimitId"), dict) else {}
        buckets: list[tuple[str, dict[str, Any]]] = []
        for limit_id, payload in rl_map.items():
            if isinstance(limit_id, str) and isinstance(payload, dict):
                buckets.append((limit_id, payload))
        if not buckets:
            payload = rate_result.get("rateLimits") if isinstance(rate_result.get("rateLimits"), dict) else {}
            if payload:
                buckets.append(("default", payload))
        if not buckets:
            lines.append("rate_limits: (unavailable)")
            return
        for limit_id, payload in buckets:
            primary = payload.get("primary") if isinstance(payload.get("primary"), dict) else {}
            secondary = payload.get("secondary") if isinstance(payload.get("secondary"), dict) else {}
            plan_type = str(payload.get("planType") or "").strip()
            if plan_type:
                lines.append(f"rate_limit[{limit_id}].plan: {plan_type}")
            for name, bucket in (("primary", primary), ("secondary", secondary)):
                if not bucket:
                    continue
                used_percent = bucket.get("usedPercent")
                remaining_percent = "(unknown)"
                if isinstance(used_percent, (int, float)):
                    remaining_percent = f"{max(0.0, 100.0 - float(used_percent)):.0f}%"
                window_mins = bucket.get("windowDurationMins")
                resets_at = self._fmt_unix_ts(bucket.get("resetsAt"))
                label = name
                if window_mins == 300:
                    label = "5h"
                elif window_mins == 10080:
                    label = "weekly"
                lines.append(f"rate_limit[{limit_id}].{label}: remaining={remaining_percent}, window={window_mins or '(unknown)'}m, resets_at={resets_at}")

    @staticmethod
    def _pick_first(d: dict[str, Any], keys: tuple[str, ...]) -> str:
        for k in keys:
            if k in d and d.get(k) is not None:
                v = str(d.get(k) or "").strip()
                if v:
                    return v
        return ""

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.info(f"cmd /status chat={update.effective_chat.id if update.effective_chat else '?'} user={update.effective_user.id if update.effective_user else '?'}")
        if not update.message:
            return
        if not self.is_allowed(update):
            await self.tg_call(lambda: update.message.reply_text("unauthorized"), timeout_s=15.0, what="/status reply")
            return
        sk = self.session_key_fn(update)
        node_id = self.get_selected_node(sk)
        if not node_id:
            await self.tg_call(lambda: update.message.reply_text("请先 /node 选择一台机器"), timeout_s=15.0, what="/status reply")
            return
        if not self.registry.is_online(node_id):
            await self.tg_call(lambda: update.message.reply_text(f"node offline: {node_id} (use /node)"), timeout_s=15.0, what="/status reply")
            return
        core = context.application.bot_data.get("core")
        if core is None:
            await self.tg_call(lambda: update.message.reply_text(f"[{node_id}] error: manager core missing"), timeout_s=15.0, what="/status reply")
            return

        placeholder = await self.tg_call(
            lambda: update.message.reply_text("trying... /status"),
            timeout_s=15.0,
            what="/status placeholder",
        )
        placeholder_id = int(getattr(placeholder, "message_id", 0) or 0)

        async def _progress(text: str) -> None:
            if placeholder_id <= 0:
                return
            try:
                await self.tg_call(
                    lambda: context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=placeholder_id,
                        text=text,
                    ),
                    timeout_s=15.0,
                    what="/status progress",
                )
            except Exception:
                return

        step = "init"
        try:
            session_model = self.get_default_model(sk)
            current_thread_id = self.get_current_thread_id(sk, node_id)
            thread_id_for_status = current_thread_id
            thread_id_source = "session"
            result_mode = self.get_result_mode(sk)

            step = "config/read"
            await _progress("trying... /status\n- config/read")
            cfg_rep = await core.appserver_call(node_id, "config/read", {}, timeout_s=min(40.0, self.task_timeout_s))
            if not bool(cfg_rep.get("ok")):
                await _progress(f"[{node_id}] error at {step}: {cfg_rep.get('error')}")
                return
            cfg_result = cfg_rep.get("result") if isinstance(cfg_rep.get("result"), dict) else {}
            cfg_obj = cfg_result.get("config") if isinstance(cfg_result.get("config"), dict) else {}

            step = "account/read"
            await _progress("trying... /status\n- account/read\n- account/rateLimits/read")
            account_rep = await core.appserver_call(node_id, "account/read", {}, timeout_s=min(40.0, self.task_timeout_s))
            step = "account/rateLimits/read"
            rate_limits_rep = await core.appserver_call(node_id, "account/rateLimits/read", {}, timeout_s=min(40.0, self.task_timeout_s))

            step = "thread/loaded/list"
            await _progress("trying... /status\n- thread/loaded/list\n- thread/list")
            loaded_rep = await core.appserver_call(node_id, "thread/loaded/list", {}, timeout_s=min(40.0, self.task_timeout_s))
            loaded_data: list[str] = []
            if bool(loaded_rep.get("ok")):
                loaded_result = loaded_rep.get("result") if isinstance(loaded_rep.get("result"), dict) else {}
                loaded_data = loaded_result.get("data") if isinstance(loaded_result.get("data"), list) else []

            step = "thread/list"
            thread_list_rep = await core.appserver_call(node_id, "thread/list", {"limit": 5}, timeout_s=min(40.0, self.task_timeout_s))
            thread_items: list[dict[str, Any]] = []
            if bool(thread_list_rep.get("ok")):
                tl_result = thread_list_rep.get("result") if isinstance(thread_list_rep.get("result"), dict) else {}
                thread_items = tl_result.get("data") if isinstance(tl_result.get("data"), list) else []
            if not thread_id_for_status and thread_items and isinstance(thread_items[0], dict):
                thread_id_for_status = str(thread_items[0].get("id") or "").strip()
                if thread_id_for_status:
                    thread_id_source = "fallback_latest"

            step = "collaborationMode/list"
            await _progress("trying... /status\n- collaborationMode/list")
            collab_rep = await core.appserver_call(node_id, "collaborationMode/list", {}, timeout_s=min(40.0, self.task_timeout_s))
            collab_modes: list[str] = []
            if bool(collab_rep.get("ok")):
                cm_result = collab_rep.get("result") if isinstance(collab_rep.get("result"), dict) else {}
                cm_data = cm_result.get("data") if isinstance(cm_result.get("data"), list) else []
                for item in cm_data:
                    if isinstance(item, dict):
                        name = str(item.get("name") or item.get("mode") or "").strip()
                        if name:
                            collab_modes.append(name)

            appserver_config_model = str(cfg_obj.get("model") or "").strip()
            next_turn_model = session_model or appserver_config_model
            thread_mode_model = ""
            thread_mode_sandbox = ""
            thread_mode_approval = ""
            if thread_id_for_status:
                step = "thread/read"
                await _progress("trying... /status\n- thread/read(current)")
                tr_rep = await core.appserver_call(
                    node_id,
                    "thread/read",
                    {"threadId": thread_id_for_status, "includeTurns": False},
                    timeout_s=min(40.0, self.task_timeout_s),
                )
                if bool(tr_rep.get("ok")):
                    tr_result = tr_rep.get("result") if isinstance(tr_rep.get("result"), dict) else {}
                    tr_thread = tr_result.get("thread") if isinstance(tr_result.get("thread"), dict) else {}
                    thread_mode_model = self._pick_first(tr_thread, ("model", "model_id"))
                    thread_mode_sandbox = self._pick_first(tr_thread, ("sandbox", "sandboxMode", "sandbox_mode"))
                    thread_mode_approval = self._pick_first(tr_thread, ("approvalPolicy", "approval_policy"))

            node_sandbox, node_approval = self.registry.get_runtime_defaults(node_id)
            lines: list[str] = []
            lines.append(f"node: {node_id}")
            lines.append(f"appserver_config_model: {appserver_config_model or '(unknown)'}")
            lines.append(f"session_model_pref: {session_model or '(none)'}")
            lines.append(f"next_turn_model: {next_turn_model or '(unknown)'}")
            lines.append(f"model_reasoning_effort: {str(cfg_obj.get('model_reasoning_effort') or '(unknown)')}")
            lines.append(f"model_reasoning_summary: {str(cfg_obj.get('model_reasoning_summary') or '(unknown)')}")
            lines.append(f"directory: {str((thread_items[0].get('cwd') if thread_items and isinstance(thread_items[0], dict) else '') or self.base_dir)}")
            lines.append(
                "thread_mode(current): "
                f"model={thread_mode_model or '(unknown)'}, "
                f"sandbox={thread_mode_sandbox or '(unknown)'}, "
                f"approval={thread_mode_approval or '(unknown)'}"
            )
            lines.append(f"execution_defaults(node): sandbox={node_sandbox or '(unknown)'}, approval={node_approval or '(unknown)'}")
            lines.append(f"agents_md: {'AGENTS.md' if (self.base_dir / 'AGENTS.md').exists() else '(none)'}")
            lines.append(f"collaboration_modes: {', '.join(collab_modes) if collab_modes else '(unknown)'}")
            lines.append(f"result_mode: {result_mode}")
            lines.append(f"session_thread: {current_thread_id or '(none)'}")
            lines.append(f"status_thread: {thread_id_for_status or '(none)'} [{thread_id_source}]")
            if bool(account_rep.get("ok")):
                account_result = account_rep.get("result") if isinstance(account_rep.get("result"), dict) else {}
                account_obj = account_result.get("account") if isinstance(account_result.get("account"), dict) else {}
                lines.append(f"account_type: {str(account_obj.get('type') or '(unknown)')}")
                email = str(account_obj.get("email") or "").strip()
                if email:
                    lines.append(f"account_email: {email}")
                plan_type = str(account_obj.get("planType") or "").strip()
                if plan_type:
                    lines.append(f"account_plan: {plan_type}")
            else:
                lines.append(f"account_error: {str(account_rep.get('error') or '(unknown)')}")
            if bool(rate_limits_rep.get("ok")):
                rate_result = rate_limits_rep.get("result") if isinstance(rate_limits_rep.get("result"), dict) else {}
                self._append_rate_limit_lines(lines, rate_result)
            else:
                lines.append(f"rate_limits_error: {str(rate_limits_rep.get('error') or '(unknown)')}")
            if thread_id_for_status:
                lines.append(f"thread_loaded: {'yes' if thread_id_for_status in loaded_data else 'no'}")

            if thread_items:
                current_item = None
                if thread_id_for_status:
                    for item in thread_items:
                        if isinstance(item, dict) and str(item.get('id') or '') == thread_id_for_status:
                            current_item = item
                            break
                if current_item is None and thread_items and isinstance(thread_items[0], dict):
                    current_item = thread_items[0]
                if isinstance(current_item, dict):
                    lines.append(f"thread_status: {str((current_item.get('status') or {}).get('type') if isinstance(current_item.get('status'), dict) else '(unknown)')}")
                    lines.append(f"thread_updated_at: {str(current_item.get('updatedAt') or '(unknown)')}")
                    preview = str(current_item.get("preview") or "").strip()
                    if preview:
                        lines.append(f"thread_preview: {preview[:160]}")
                    cli_ver = str(current_item.get("cliVersion") or "").strip()
                    if cli_ver:
                        lines.append(f"cli_version: {cli_ver}")
                    git_info = current_item.get("gitInfo") if isinstance(current_item.get("gitInfo"), dict) else {}
                    branch = str(git_info.get("branch") or "").strip()
                    sha = str(git_info.get("sha") or "").strip()
                    if branch or sha:
                        lines.append(f"git: branch={branch or '?'} sha={sha[:12] if sha else '?'}")

            lines.append(f"loaded_threads: {len(loaded_data)}")
            final_text = "\n".join(lines)
            if placeholder_id > 0:
                try:
                    await self.tg_call(
                        lambda: context.bot.edit_message_text(
                            chat_id=update.effective_chat.id,
                            message_id=placeholder_id,
                            text=final_text,
                        ),
                        timeout_s=15.0,
                        what="/status final",
                    )
                    return
                except Exception:
                    pass
            await self.tg_call(lambda: update.message.reply_text(final_text), timeout_s=15.0, what="/status reply")
        except asyncio.TimeoutError:
            await _progress(f"[{node_id}] timeout at step: {step}")
            return
        except Exception as e:
            await _progress(f"[{node_id}] error at step {step}: {type(e).__name__}: {e}")
            return
