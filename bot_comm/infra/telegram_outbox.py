from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

from telegram.error import NetworkError, TimedOut


@dataclass
class TgAction:
    type: str  # "edit" | "send" | "typing"
    chat_id: int
    text: str = ""
    message_id: int | None = None
    reply_markup: Any | None = None
    timeout_s: float = 15.0
    trace_id: str = ""
    node_id: str = ""
    task_id: str = ""
    kind: str = ""  # "placeholder" | "result" | "late" | "timeout" | ...
    retries_left: int | None = None
    backoff_s: float = 1.0


class TelegramOutbox:
    def __init__(
        self,
        bot: Any,
        *,
        tg_call: Callable[..., Any],
        logger: Any,
        max_active_chats: int = 200,
        max_queue_per_chat: int = 50,
        idle_ttl_s: float = 600.0,
    ) -> None:
        self.bot = bot
        self.tg_call = tg_call
        self.logger = logger
        self.max_active_chats = max_active_chats
        self.max_queue_per_chat = max_queue_per_chat
        self.idle_ttl_s = idle_ttl_s
        self._lock = asyncio.Lock()
        self._queues: dict[int, asyncio.Queue[TgAction]] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._last_active: dict[int, float] = {}
        self._last_edit_text: dict[tuple[int, int], str] = {}

    @staticmethod
    def _is_low_priority(action: TgAction) -> bool:
        return action.type == "typing" or action.kind in ("progress", "progress_batch")

    async def enqueue(self, action: TgAction) -> bool:
        now = time.time()
        if action.retries_left is None:
            if self._is_low_priority(action):
                action.retries_left = 0
            elif action.kind in ("result", "result_done", "manager_error", "timeout", "approval"):
                action.retries_left = 12
            else:
                action.retries_left = 6
        async with self._lock:
            q = self._queues.get(action.chat_id)
            if q is None:
                if len(self._queues) >= self.max_active_chats:
                    return False
                q = asyncio.Queue(maxsize=self.max_queue_per_chat)
                self._queues[action.chat_id] = q
                self._last_active[action.chat_id] = now
                self._tasks[action.chat_id] = asyncio.create_task(self._sender_loop(action.chat_id), name=f"tg_sender:{action.chat_id}")
            self._last_active[action.chat_id] = now
            try:
                q.put_nowait(action)
            except asyncio.QueueFull:
                # Keep result/error delivery reliable: drop low-priority progress first.
                if self._is_low_priority(action):
                    return False
                dropped = False
                try:
                    # asyncio.Queue stores items in a deque at _queue.
                    # We surgically drop one low-priority item to make room for high-priority results.
                    for i, old in enumerate(q._queue):  # type: ignore[attr-defined]
                        if isinstance(old, TgAction) and self._is_low_priority(old):
                            del q._queue[i]  # type: ignore[attr-defined]
                            dropped = True
                            break
                except Exception:
                    dropped = False
                if not dropped:
                    return False
                try:
                    q.put_nowait(action)
                except asyncio.QueueFull:
                    return False
            return True

    async def _sender_loop(self, chat_id: int) -> None:
        try:
            while True:
                async with self._lock:
                    last = self._last_active.get(chat_id, time.time())
                    q = self._queues.get(chat_id)
                if q is None:
                    return
                if q.empty() and (time.time() - last) > self.idle_ttl_s:
                    async with self._lock:
                        self._queues.pop(chat_id, None)
                        self._last_active.pop(chat_id, None)
                        self._tasks.pop(chat_id, None)
                    return

                try:
                    action = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                try:
                    if action.type == "edit":
                        if action.message_id is None:
                            raise ValueError("edit action missing message_id")
                        key = (action.chat_id, action.message_id)
                        last_text = self._last_edit_text.get(key)
                        if last_text == action.text:
                            continue
                        await self.tg_call(
                            lambda: self.bot.edit_message_text(
                                chat_id=action.chat_id,
                                message_id=action.message_id,
                                text=action.text,
                                reply_markup=action.reply_markup,
                            ),
                            timeout_s=action.timeout_s,
                            what="tg edit",
                            retries=1,
                        )
                        self._last_edit_text[key] = action.text
                        self.logger.info(
                            f"op=tg.edit ok=true chat_id={action.chat_id} msg_id={action.message_id} "
                            f"trace_id={action.trace_id} node_id={action.node_id} task_id={action.task_id} kind={action.kind}"
                        )
                    elif action.type == "send":
                        await self.tg_call(
                            lambda: self.bot.send_message(chat_id=action.chat_id, text=action.text, reply_markup=action.reply_markup),
                            timeout_s=action.timeout_s,
                            what="tg send",
                            retries=1,
                        )
                        self.logger.info(
                            f"op=tg.send ok=true chat_id={action.chat_id} trace_id={action.trace_id} "
                            f"node_id={action.node_id} task_id={action.task_id} kind={action.kind}"
                        )
                    elif action.type == "typing":
                        await self.tg_call(
                            lambda: self.bot.send_chat_action(chat_id=action.chat_id, action="typing"),
                            timeout_s=action.timeout_s,
                            what="tg typing",
                            retries=0,
                        )
                        self.logger.info(
                            f"op=tg.typing ok=true chat_id={action.chat_id} trace_id={action.trace_id} "
                            f"node_id={action.node_id} task_id={action.task_id} kind={action.kind}"
                        )
                except (TimedOut, NetworkError, asyncio.TimeoutError) as e:
                    left = int(action.retries_left or 0)
                    if left > 0:
                        action.retries_left = left - 1
                        delay = max(0.5, float(action.backoff_s or 1.0))
                        action.backoff_s = min(30.0, delay * 1.7)
                        self.logger.info(
                            f"tg outbox transient failed chat={chat_id}: {type(e).__name__}: {e} "
                            f"(will retry in {delay:.1f}s, left={action.retries_left})"
                        )
                        await asyncio.sleep(delay)
                        try:
                            q.put_nowait(action)
                        except asyncio.QueueFull:
                            self.logger.warning(f"tg outbox retry dropped chat={chat_id}: queue full")
                        continue
                    self.logger.warning(f"tg outbox send failed chat={chat_id}: {type(e).__name__}: {e}")
                except Exception as e:
                    self.logger.warning(f"tg outbox send failed chat={chat_id}: {type(e).__name__}: {e}")
                finally:
                    async with self._lock:
                        self._last_active[chat_id] = time.time()
        except asyncio.CancelledError:
            raise
