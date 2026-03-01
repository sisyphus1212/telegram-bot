from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot_comm.infra.telegram_outbox import TelegramOutbox, TgAction
from manager.service.dispatch_service import DispatchService
from manager.service.progress_render_service import ProgressRenderService
from manager.service.task_models import ApprovalContext, NodePresenceRecord, TaskContext

JsonDict = dict[str, Any]


def _split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        j = min(i + limit, n)
        k = text.rfind("\n", i, j)
        if k <= i:
            k = text.rfind(" ", i, j)
        if k <= i:
            k = j
        out.append(text[i:k])
        i = k
        while i < n and text[i] in " \n":
            i += 1
    return out if out else [""]


def _prefix_and_split_telegram_text(text: str, *, prefix: str, limit: int = 3900) -> list[str]:
    text = text or ""
    prefix = prefix or ""
    if len(prefix) >= limit:
        return _split_telegram_text(prefix + text, limit=limit)
    per_part_limit = max(200, limit - len(prefix))
    parts = _split_telegram_text(text, limit=per_part_limit)
    return [prefix + p for p in parts]


def _short_one_line(s: str, limit: int = 80) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 3)] + "..."


def _result_fingerprint(text: str, *, parts: int) -> str:
    body = text or ""
    head = _short_one_line(body[:200], limit=60)
    tail = _short_one_line(body[-200:], limit=60) if len(body) > 0 else ""
    ln = len(body)
    return f'result: len={ln} parts={parts} head="{head}" tail="{tail}"'


class ManagerCore:
    def __init__(
        self,
        registry: Any,
        *,
        task_timeout_s: float,
        base_dir: Path,
        logger: Any,
        node_notify_chat_ids: list[int] | None = None,
        node_notify_min_interval_s: float = 60.0,
        recent_ttl_s: float = 1800.0,
        recent_max: int = 10000,
    ) -> None:
        self.registry = registry
        self.base_dir = base_dir
        self.logger = logger
        self.task_timeout_s = float(task_timeout_s)
        self.node_notify_chat_ids = node_notify_chat_ids or []
        self.node_notify_min_interval_s = float(node_notify_min_interval_s)
        self.recent_ttl_s = float(recent_ttl_s)
        self.recent_max = int(recent_max)

        self._lock = asyncio.Lock()
        self._tasks_inflight: dict[str, TaskContext] = {}
        self._recent: "OrderedDict[str, tuple[TaskContext, float]]" = OrderedDict()
        self._outbox: TelegramOutbox | None = None
        self._timeout_task: asyncio.Task | None = None
        self._waiters: dict[str, asyncio.Future[JsonDict]] = {}
        self._rpc_waiters: dict[str, asyncio.Future[JsonDict]] = {}
        self._approvals: dict[str, ApprovalContext] = {}
        self._node_presence: dict[str, NodePresenceRecord] = {}
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._draining = False
        self.progress_update_interval_s = 5.0
        self.progress_render = ProgressRenderService()
        self.dispatch_service = DispatchService(
            registry=self.registry,
            waiters=self._waiters,
            rpc_waiters=self._rpc_waiters,
            lock=self._lock,
            base_dir=self.base_dir,
            logger=self.logger,
        )

    def set_outbox(self, outbox: TelegramOutbox) -> None:
        self._outbox = outbox
        if self._timeout_task is None:
            self._timeout_task = asyncio.create_task(self._timeout_loop(), name="task_timeout_loop")
        # Flush any pending node-online notifications that happened before TG was ready.
        asyncio.create_task(self._flush_pending_node_online_notifications(), name="flush_node_online_notifies")

    async def _flush_pending_node_online_notifications(self) -> None:
        # Best-effort: if outbox is missing, there is nothing to do.
        if self._outbox is None or not self.node_notify_chat_ids:
            return
        # Snapshot current online nodes and presence records.
        async with self._lock:
            online_ids = set(self.registry.online_node_ids())
            records = list(self._node_presence.values())
        for rec in records:
            if not rec.online:
                continue
            if rec.node_id not in online_ids:
                continue
            if rec.pending_online_notify or rec.last_notify_at <= 0.0:
                await self._notify_node_online(node_id=rec.node_id, peer=rec.last_peer, host_name=rec.host_name, force=True)

    async def tg_send(self, *, chat_id: int, text: str, kind: str, timeout_s: float = 8.0, retries_left: int | None = None) -> bool:
        outbox = self._outbox
        if outbox is None:
            return False
        return await outbox.enqueue(TgAction(type="send", chat_id=chat_id, text=text, kind=kind, timeout_s=timeout_s, retries_left=retries_left))

    def _format_node_online_text(self, *, node_id: str, peer: str, host_name: str = "", event: str = "online") -> str:
        # Keep message short and mobile-friendly.
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        peer_s = peer or "?"
        hn = host_name.strip()
        host_s = f" host={hn}" if hn else ""
        title = "node reconnected" if event == "reconnected" else "node online"
        return f"[{title}] {node_id}{host_s} peer={peer_s} at {ts}\n\nTips: /node to select"

    async def _notify_node_online(
        self,
        *,
        node_id: str,
        peer: str,
        host_name: str = "",
        force: bool = False,
        event: str = "online",
    ) -> None:
        now = time.time()
        async with self._lock:
            rec = self._node_presence.get(node_id)
            if rec is None:
                rec = NodePresenceRecord(
                    node_id=node_id,
                    first_seen_at=now,
                    last_seen_at=now,
                    online=True,
                    last_online_at=now,
                    last_peer=peer or "",
                    host_name=host_name or "",
                )
                self._node_presence[node_id] = rec
            # Rate-limit per node.
            if not force and (now - rec.last_notify_at) < max(5.0, self.node_notify_min_interval_s):
                return
        if not self.node_notify_chat_ids:
            return
        text = self._format_node_online_text(node_id=node_id, peer=peer, host_name=host_name, event=event)
        ok_any = False
        for chat_id in self.node_notify_chat_ids:
            ok_any = (await self.tg_send(chat_id=int(chat_id), text=text, kind="node_online", timeout_s=15.0, retries_left=6)) or ok_any
        async with self._lock:
            rec = self._node_presence.get(node_id)
            if rec is None:
                return
            if ok_any:
                rec.last_notify_at = now
                rec.pending_online_notify = False
            else:
                rec.pending_online_notify = True

    async def on_node_online(self, node_id: str, *, peer: str = "", host_name: str = "") -> None:
        now = time.time()
        event = "online"
        async with self._lock:
            rec = self._node_presence.get(node_id)
            if rec is None:
                rec = NodePresenceRecord(node_id=node_id, first_seen_at=now, last_seen_at=now)
                self._node_presence[node_id] = rec
            elif rec.online:
                event = "reconnected"
            rec.last_seen_at = now
            rec.last_peer = peer or rec.last_peer
            rec.host_name = host_name or rec.host_name
            rec.online = True
            rec.last_online_at = now
            rec.online_count += 1
            rec.pending_online_notify = True
        # If TG isn't ready yet, keep pending_online_notify=true; it'll be flushed in set_outbox().
        if self._outbox is None:
            return
        await self._notify_node_online(node_id=node_id, peer=peer, host_name=host_name, force=True, event=event)

    async def on_node_offline(self, node_id: str) -> None:
        now = time.time()
        async with self._lock:
            rec = self._node_presence.get(node_id)
            if rec is None:
                rec = NodePresenceRecord(node_id=node_id, first_seen_at=now, last_seen_at=now)
                self._node_presence[node_id] = rec
            rec.last_seen_at = now
            if rec.online:
                rec.online = False
                rec.last_offline_at = now
                rec.offline_count += 1

    async def register_task(self, ctx: TaskContext) -> None:
        if self._draining:
            raise RuntimeError("manager draining")
        async with self._lock:
            if ctx.progress_lines is None:
                ctx.progress_lines = []
            self._tasks_inflight[ctx.task_id] = ctx
            self._gc_recent_locked()
        self._start_typing_for_task(ctx)

    def is_draining(self) -> bool:
        return self._draining

    async def begin_drain(self) -> None:
        self._draining = True

    async def wait_drained(self, timeout_s: float) -> int:
        deadline = time.time() + max(0.0, float(timeout_s))
        while True:
            async with self._lock:
                pending = len(self._tasks_inflight)
            if pending <= 0 or time.time() >= deadline:
                return pending
            await asyncio.sleep(0.2)

    async def get_runtime_status(self) -> dict[str, int | bool]:
        async with self._lock:
            inflight = len(self._tasks_inflight)
            approvals = len(self._approvals)
        return {
            "draining": bool(self._draining),
            "inflight_tasks": int(inflight),
            "pending_approvals": int(approvals),
        }

    def _start_typing_for_task(self, ctx: TaskContext) -> None:
        # Keep "typing" alive while task is inflight. Telegram expires this state quickly.
        self._stop_typing_for_task(ctx.task_id)

        async def _typing_loop() -> None:
            try:
                # slight delay to avoid racing with placeholder send
                await asyncio.sleep(0.6)
                while True:
                    async with self._lock:
                        if ctx.task_id not in self._tasks_inflight:
                            return
                    outbox = self._outbox
                    if outbox is not None:
                        await outbox.enqueue(
                            TgAction(
                                type="typing",
                                chat_id=ctx.chat_id,
                                trace_id=ctx.trace_id,
                                node_id=ctx.node_id,
                                task_id=ctx.task_id,
                                kind="typing",
                                timeout_s=8.0,
                            )
                        )
                    await asyncio.sleep(4.0)
            except asyncio.CancelledError:
                raise

        self._typing_tasks[ctx.task_id] = asyncio.create_task(_typing_loop(), name=f"tg_typing:{ctx.task_id}")

    def _stop_typing_for_task(self, task_id: str) -> None:
        t = self._typing_tasks.pop(task_id, None)
        if t and not t.done():
            t.cancel()

    async def dispatch_once(self, node_id: str, prompt: str, *, timeout_s: float, model: str = "", effort: str = "") -> JsonDict:
        if self._draining:
            raise RuntimeError("manager draining")
        return await self.dispatch_service.dispatch_once(
            node_id,
            prompt,
            timeout_s=timeout_s,
            appserver_call=lambda nid, method, params: self.appserver_call(nid, method, params, timeout_s=min(timeout_s, 60.0)),
            model=model,
            effort=effort,
        )

    async def dispatch_task(self, node_id: str, thread_id: str, prompt: str, *, thread_key: str, timeout_s: float, model: str = "", effort: str = "") -> JsonDict:
        if self._draining:
            raise RuntimeError("manager draining")
        return await self.dispatch_service.dispatch_task(
            node_id,
            thread_id,
            prompt,
            thread_key=thread_key,
            timeout_s=timeout_s,
            model=model,
            effort=effort,
        )

    async def appserver_call(self, node_id: str, method: str, params: JsonDict | None, *, timeout_s: float = 30.0) -> JsonDict:
        if self._draining:
            raise RuntimeError("manager draining")
        return await self.dispatch_service.appserver_call(node_id, method, params, timeout_s=timeout_s)

    async def send_task_assign(self, *, node_id: str, task_msg: JsonDict) -> None:
        await self.dispatch_service.send_task_assign(
            node_id=node_id,
            task_msg=task_msg,
            fail_task=lambda task_id, reason: self._fail_task(task_id, reason, node_id=node_id),
        )

    async def on_node_message(self, node_id: str, msg: JsonDict) -> None:
        t = msg.get("type")
        if t == "approval_request":
            await self._handle_approval_request(node_id=node_id, msg=msg)
            return
        if t == "task_progress":
            await self._handle_task_progress(node_id=node_id, msg=msg)
            return
        if t == "task_ack":
            task_id = str(msg.get("task_id") or "")
            trace_id = str(msg.get("trace_id") or "")
            self.logger.info(f"op=ws.recv node_id={node_id} type=task_ack trace_id={trace_id} task_id={task_id}")
            return
        if t == "appserver_response":
            req_id = str(msg.get("req_id") or "")
            trace_id = str(msg.get("trace_id") or "")
            self.logger.info(f"op=ws.recv node_id={node_id} type=appserver_response trace_id={trace_id} req_id={req_id} ok={bool(msg.get('ok'))}")
            if not req_id:
                return
            async with self._lock:
                waiter = self._rpc_waiters.get(req_id)
                if waiter and not waiter.done():
                    waiter.set_result(msg)
                    return
            self.logger.info(f"orphan appserver_response: node={node_id} req_id={req_id} ok={bool(msg.get('ok'))}")
            return
        if t != "task_result":
            return
        task_id = str(msg.get("task_id") or "")
        if not task_id:
            return
        trace_id = str(msg.get("trace_id") or "")
        self.logger.info(f"op=ws.recv node_id={node_id} type=task_result trace_id={trace_id} task_id={task_id} ok={bool(msg.get('ok'))}")

        async with self._lock:
            waiter = self._waiters.get(task_id)
            if waiter and not waiter.done():
                waiter.set_result(msg)
                return
        await self._handle_task_result(node_id=node_id, msg=msg)

    async def _handle_task_progress(self, *, node_id: str, msg: JsonDict) -> None:
        task_id = str(msg.get("task_id") or "")
        trace_id = str(msg.get("trace_id") or "")
        event = str(msg.get("event") or "")
        summary = str(msg.get("summary") or "").strip()
        force = bool(msg.get("force"))
        if not task_id or not summary:
            return
        self.logger.info(f"op=ws.recv node_id={node_id} type=task_progress trace_id={trace_id} task_id={task_id} event={event} force={force} summary={summary[:200]}")

        ctx: TaskContext | None = None
        text_to_send = ""
        send_batch = False
        batch_size = 5
        async with self._lock:
            ctx = self._tasks_inflight.get(task_id)
            if ctx is None:
                return
            now = time.time()
            ctx.last_progress_seen_at = now
            if ctx.progress_lines is None:
                ctx.progress_lines = []
            stage = str(msg.get("stage") or "")
            # Treat retrying as internal liveness signal; don't spam TG with reconnect noise.
            if stage == "retrying" and not force:
                ctx.last_progress_event = event
                return
            label = self.progress_render.normalize_progress_line(event=event, stage=stage, summary=summary, ctx=ctx, now=now)
            if label is not None:
                before_last = ctx.progress_lines[-1] if ctx.progress_lines else ""
                self.progress_render.push_progress_line(ctx.progress_lines, label)
                if label != before_last:
                    ctx.progress_change_count += 1
            ctx.pending_progress_text = summary
            ctx.last_progress_event = event
            unsent_changes = ctx.progress_change_count - ctx.progress_last_batch_sent_count
            if unsent_changes < batch_size and not (force and unsent_changes > 0):
                return
            send_batch = True
            text_to_send = self.progress_render.render_progress_batch_text(ctx, batch_size=batch_size)
            ctx.last_progress_at = now
            ctx.last_progress_text = ctx.pending_progress_text
            ctx.pending_progress_text = ""
            ctx.progress_last_batch_sent_count = ctx.progress_change_count

        outbox = self._outbox
        if outbox is None or ctx is None or not send_batch:
            return
        if not await outbox.enqueue(
            TgAction(
                type="send",
                chat_id=ctx.chat_id,
                text=text_to_send,
                trace_id=trace_id or ctx.trace_id,
                node_id=node_id,
                task_id=task_id,
                kind="progress_batch",
            )
        ):
            self.logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (progress_batch)")

    async def _handle_approval_request(self, *, node_id: str, msg: JsonDict) -> None:
        approval_id = str(msg.get("approval_id") or "")
        task_id = str(msg.get("task_id") or "")
        trace_id = str(msg.get("trace_id") or "")
        method = str(msg.get("method") or "")
        params = msg.get("params")
        rpc_id = msg.get("rpc_id")
        if not approval_id or not method:
            return
        if not isinstance(params, dict):
            params = {}
        rpc_id_i = rpc_id if isinstance(rpc_id, int) else None

        ctx: TaskContext | None = None
        async with self._lock:
            # We only support approvals triggered during an inflight task for now.
            ctx = self._tasks_inflight.get(task_id) if task_id else None
        if ctx is None:
            self.logger.warning(f"approval_request dropped: node={node_id} approval_id={approval_id} task_id={task_id!r} method={method}")
            # Don't hang the node: decline immediately.
            try:
                await self.registry.send_json(node_id, {"type": "approval_decision", "approval_id": approval_id, "decision": "decline"})
            except Exception:
                pass
            return

        outbox = self._outbox
        if outbox is None:
            self.logger.warning(f"approval_request dropped (no outbox): node={node_id} approval_id={approval_id}")
            try:
                await self.registry.send_json(node_id, {"type": "approval_decision", "approval_id": approval_id, "decision": "decline"})
            except Exception:
                pass
            return

        ac = ApprovalContext(
            approval_id=approval_id,
            node_id=node_id,
            task_id=task_id,
            trace_id=trace_id or ctx.trace_id,
            rpc_id=rpc_id_i,
            method=method,
            params=params,
            chat_id=ctx.chat_id,
            created_at=time.time(),
        )
        async with self._lock:
            self._approvals[approval_id] = ac

        # Render a minimal approval message (align with app-server naming).
        text_lines: list[str] = []
        text_lines.append(f"[{node_id}] 需要审批：{method}")
        # Try to extract key fields from the official request payload.
        if method == "item/commandExecution/requestApproval":
            cmd = params.get("command")
            cwd = params.get("cwd")
            reason = params.get("reason")
            if cmd:
                text_lines.append(f"command: {cmd}")
            if cwd:
                text_lines.append(f"cwd: {cwd}")
            if reason:
                text_lines.append(f"reason: {reason}")
            net = params.get("networkApprovalContext")
            if isinstance(net, dict):
                host = net.get("host")
                proto = net.get("protocol")
                port = net.get("port")
                if host or proto or port:
                    text_lines.append(f"network: {proto or '?'} {host or '?'}:{port or '?'}")
        elif method == "item/fileChange/requestApproval":
            reason = params.get("reason")
            if reason:
                text_lines.append(f"reason: {reason}")
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data=f"approve:accept:{approval_id}"),
                    InlineKeyboardButton("Decline", callback_data=f"approve:decline:{approval_id}"),
                ],
                [InlineKeyboardButton("Approve (Session)", callback_data=f"approve:session:{approval_id}")],
            ]
        )
        # Send details and actions separately so button card stays compact and never gets split.
        await outbox.enqueue(
            TgAction(
                type="send",
                chat_id=ctx.chat_id,
                text="\n".join(text_lines),
                trace_id=ac.trace_id,
                node_id=node_id,
                task_id=task_id,
                kind="approval_detail",
            )
        )
        await outbox.enqueue(
            TgAction(
                type="send",
                chat_id=ctx.chat_id,
                text=f"[{node_id}] 审批操作\napproval_id: {approval_id}",
                reply_markup=kb,
                trace_id=ac.trace_id,
                node_id=node_id,
                task_id=task_id,
                kind="approval",
            )
        )

    async def approval_decide(self, *, approval_id: str, decision: str, chat_id: int) -> tuple[bool, str]:
        decision = (decision or "").strip()
        if decision not in ("accept", "acceptForSession", "decline", "cancel"):
            return False, "invalid decision"
        async with self._lock:
            ac = self._approvals.get(approval_id)
        if ac is None:
            return False, "approval_id not found (maybe expired)"
        if ac.chat_id != chat_id:
            return False, "chat mismatch"
        try:
            await self.registry.send_json(ac.node_id, {"type": "approval_decision", "approval_id": approval_id, "decision": decision})
        except Exception as e:
            return False, f"send failed: {type(e).__name__}: {e}"
        async with self._lock:
            self._approvals.pop(approval_id, None)
        return True, "ok"

    async def _handle_task_result(self, *, node_id: str, msg: JsonDict) -> None:
        task_id = str(msg.get("task_id") or "")
        trace_id = str(msg.get("trace_id") or "")
        ok = bool(msg.get("ok"))
        text = str(msg.get("text") or "")
        err = str(msg.get("error") or "")
        error_kind = str(msg.get("error_kind") or "").strip()
        retriable = msg.get("retriable")
        if not task_id:
            return

        ctx: TaskContext | None = None
        late_ctx: TaskContext | None = None
        async with self._lock:
            ctx = self._tasks_inflight.pop(task_id, None)
            if ctx is None:
                item = self._recent.get(task_id)
                if item:
                    late_ctx = item[0]
            if ctx is not None:
                self._recent[task_id] = (ctx, time.time())
                self._recent.move_to_end(task_id)
                self._gc_recent_locked()
        self._stop_typing_for_task(task_id)

        outbox = self._outbox
        if outbox is None:
            self.logger.warning(f"task_result dropped (no outbox): node={node_id} task_id={task_id} ok={ok}")
            return

        if ctx is None and late_ctx is not None:
            body = text.strip() if ok else f"error: {err or 'unknown error'}"
            body = body or "(empty)"
            parts = _prefix_and_split_telegram_text(body, prefix=f"[{node_id}][late task_id={task_id}] ")
            for p in parts:
                await outbox.enqueue(
                    TgAction(type="send", chat_id=late_ctx.chat_id, text=p, trace_id=trace_id or late_ctx.trace_id, node_id=node_id, task_id=task_id, kind="late")
                )
            return

        if ctx is None:
            self.logger.info(f"orphan task_result: node={node_id} task_id={task_id} ok={ok} err={err!r}")
            return

        if ok:
            body = text.strip() or "(empty)"
            parts = _prefix_and_split_telegram_text(body, prefix=f"[{node_id}] ")
            if ctx.result_mode == "send":
                fp = _result_fingerprint(body, parts=len(parts))
                done_text = self.progress_render.render_progress_done_text(ctx, ok=True) + "\n" + fp
                if not await outbox.enqueue(
                    TgAction(
                        type="edit",
                        chat_id=ctx.chat_id,
                        message_id=ctx.placeholder_msg_id,
                        text=done_text,
                        trace_id=trace_id or ctx.trace_id,
                        node_id=node_id,
                        task_id=task_id,
                        kind="result_done",
                    )
                ):
                    self.logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (edit done)")
                for extra in parts:
                    if not await outbox.enqueue(
                        TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=trace_id or ctx.trace_id, node_id=node_id, task_id=task_id, kind="result")
                    ):
                        self.logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (send result)")
            else:
                if not await outbox.enqueue(
                    TgAction(
                        type="edit",
                        chat_id=ctx.chat_id,
                        message_id=ctx.placeholder_msg_id,
                        text=parts[0],
                        trace_id=trace_id or ctx.trace_id,
                        node_id=node_id,
                        task_id=task_id,
                        kind="result",
                    )
                ):
                    self.logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (edit)")
                for extra in parts[1:]:
                    if not await outbox.enqueue(
                        TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=trace_id or ctx.trace_id, node_id=node_id, task_id=task_id, kind="result")
                    ):
                        self.logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (send extra)")
            return

        details: list[str] = []
        if error_kind:
            details.append(f"kind={error_kind}")
        if isinstance(retriable, bool):
            details.append(f"retriable={'yes' if retriable else 'no'}")
        detail_s = f" ({', '.join(details)})" if details else ""
        body = f"error{detail_s}: {err or 'unknown error'}"
        parts = _prefix_and_split_telegram_text(body, prefix=f"[{node_id}] ")
        if ctx.result_mode == "send":
            done_text = self.progress_render.render_progress_done_text(ctx, ok=False)
            if not await outbox.enqueue(
                TgAction(
                    type="edit",
                    chat_id=ctx.chat_id,
                    message_id=ctx.placeholder_msg_id,
                    text=done_text,
                    trace_id=trace_id or ctx.trace_id,
                    node_id=node_id,
                    task_id=task_id,
                    kind="result_done",
                )
            ):
                self.logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (edit err done)")
            for extra in parts:
                if not await outbox.enqueue(
                    TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=trace_id or ctx.trace_id, node_id=node_id, task_id=task_id, kind="result")
                ):
                    self.logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (send err)")
        else:
            if not await outbox.enqueue(
                TgAction(
                    type="edit",
                    chat_id=ctx.chat_id,
                    message_id=ctx.placeholder_msg_id,
                    text=parts[0],
                    trace_id=trace_id or ctx.trace_id,
                    node_id=node_id,
                    task_id=task_id,
                    kind="result",
                )
            ):
                self.logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (edit err)")
            for extra in parts[1:]:
                if not await outbox.enqueue(
                    TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=trace_id or ctx.trace_id, node_id=node_id, task_id=task_id, kind="result")
                ):
                    self.logger.warning(f"tg outbox enqueue failed chat={ctx.chat_id} (send extra err)")

    async def _fail_task(self, task_id: str, reason: str, *, node_id: str) -> None:
        ctx: TaskContext | None = None
        async with self._lock:
            ctx = self._tasks_inflight.pop(task_id, None)
            if ctx is not None:
                self._recent[task_id] = (ctx, time.time())
                self._recent.move_to_end(task_id)
                self._gc_recent_locked()
        self._stop_typing_for_task(task_id)
        outbox = self._outbox
        if outbox is None or ctx is None:
            return
        parts = _prefix_and_split_telegram_text(f"error: {reason}", prefix=f"[{node_id}] ")
        await outbox.enqueue(
            TgAction(
                type="edit",
                chat_id=ctx.chat_id,
                message_id=ctx.placeholder_msg_id,
                text=parts[0],
                trace_id=ctx.trace_id,
                node_id=node_id,
                task_id=task_id,
                kind="manager_error",
            )
        )
        for extra in parts[1:]:
            await outbox.enqueue(
                TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=ctx.trace_id, node_id=node_id, task_id=task_id, kind="manager_error")
            )

    async def _timeout_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            expired: list[tuple[str, TaskContext]] = []
            async with self._lock:
                for tid, ctx in list(self._tasks_inflight.items()):
                    # Activity-based timeout: reset the timer on any progress event.
                    last = ctx.last_progress_seen_at or ctx.created_at
                    timeout_s = max(30.0, float(getattr(ctx, "timeout_s", self.task_timeout_s) or self.task_timeout_s))
                    if (now - last) > timeout_s:
                        expired.append((tid, ctx))
                        self._tasks_inflight.pop(tid, None)
                        self._recent[tid] = (ctx, now)
                        self._recent.move_to_end(tid)
                self._gc_recent_locked()
            if not expired:
                continue
            outbox = self._outbox
            if outbox is None:
                continue
            for _tid, ctx in expired:
                self._stop_typing_for_task(_tid)
                parts = _prefix_and_split_telegram_text("error: timeout", prefix=f"[{ctx.node_id}] ")
                await outbox.enqueue(
                    TgAction(
                        type="edit",
                        chat_id=ctx.chat_id,
                        message_id=ctx.placeholder_msg_id,
                        text=parts[0],
                        trace_id=ctx.trace_id,
                        node_id=ctx.node_id,
                        task_id=_tid,
                        kind="timeout",
                    )
                )
                for extra in parts[1:]:
                    await outbox.enqueue(
                        TgAction(type="send", chat_id=ctx.chat_id, text=extra, trace_id=ctx.trace_id, node_id=ctx.node_id, task_id=_tid, kind="timeout")
                    )

    def _gc_recent_locked(self) -> None:
        now = time.time()
        while self._recent:
            tid, (_ctx, ts) = next(iter(self._recent.items()))
            if (now - ts) <= self.recent_ttl_s:
                break
            self._recent.pop(tid, None)
        while len(self._recent) > self.recent_max:
            self._recent.popitem(last=False)
