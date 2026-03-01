from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable


JsonDict = dict[str, Any]


class DispatchService:
    def __init__(
        self,
        *,
        registry: Any,
        waiters: dict[str, asyncio.Future[JsonDict]],
        rpc_waiters: dict[str, asyncio.Future[JsonDict]],
        lock: asyncio.Lock,
        base_dir: Path,
        logger: Any,
    ) -> None:
        self.registry = registry
        self.waiters = waiters
        self.rpc_waiters = rpc_waiters
        self.lock = lock
        self.base_dir = base_dir
        self.logger = logger

    async def dispatch_once(
        self,
        node_id: str,
        prompt: str,
        *,
        timeout_s: float,
        appserver_call: Callable[[str, str, JsonDict | None], Awaitable[JsonDict]],
        model: str = "",
        effort: str = "",
    ) -> JsonDict:
        rep = await appserver_call(
            node_id,
            "thread/start",
            {"cwd": str(self.base_dir), "personality": "pragmatic"},
        )
        if not bool(rep.get("ok")):
            raise RuntimeError(f"thread/start failed: {rep.get('error')}")
        result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise RuntimeError(f"thread/start missing id: {result!r}")
        return await self.dispatch_task(node_id, thread_id, prompt, thread_key="probe:local", timeout_s=timeout_s, model=model, effort=effort)

    async def dispatch_task(
        self,
        node_id: str,
        thread_id: str,
        prompt: str,
        *,
        thread_key: str,
        timeout_s: float,
        model: str = "",
        effort: str = "",
    ) -> JsonDict:
        task_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        fut: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        async with self.lock:
            self.waiters[task_id] = fut
        msg: JsonDict = {
            "type": "task_assign",
            "trace_id": trace_id,
            "task_id": task_id,
            "thread_key": thread_key,
            "thread_id": thread_id,
            "prompt": prompt,
            "timeout_s": float(timeout_s),
        }
        if model:
            msg["model"] = model
        if effort:
            msg["effort"] = effort
        t0 = time.time()
        self.logger.info(f"op=dispatch.enqueue node_id={node_id} trace_id={trace_id} task_id={task_id} thread_id={thread_id[-8:]} prompt_len={len(prompt)}")
        await self.registry.send_json(node_id, msg)
        try:
            res = await asyncio.wait_for(fut, timeout=timeout_s)
            latency_ms = int((time.time() - t0) * 1000.0)
            self.logger.info(
                f"op=dispatch.done node_id={node_id} trace_id={trace_id} task_id={task_id} thread_id={thread_id[-8:]} "
                f"ok={bool(res.get('ok'))} latency_ms={latency_ms}"
            )
            return res
        finally:
            async with self.lock:
                self.waiters.pop(task_id, None)

    async def appserver_call(
        self,
        node_id: str,
        method: str,
        params: JsonDict | None,
        *,
        timeout_s: float = 30.0,
    ) -> JsonDict:
        req_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        fut: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        async with self.lock:
            self.rpc_waiters[req_id] = fut
        msg: JsonDict = {"type": "appserver_request", "req_id": req_id, "trace_id": trace_id, "method": method, "params": params or {}}
        t0 = time.time()
        self.logger.info(f"op=appserver.send node_id={node_id} trace_id={trace_id} req_id={req_id} method={method}")
        try:
            await self.registry.send_json(node_id, msg)
            res = await asyncio.wait_for(fut, timeout=timeout_s)
            latency_ms = int((time.time() - t0) * 1000.0)
            self.logger.info(f"op=appserver.done node_id={node_id} trace_id={trace_id} req_id={req_id} ok={bool(res.get('ok'))} latency_ms={latency_ms}")
            return res
        finally:
            async with self.lock:
                self.rpc_waiters.pop(req_id, None)

    async def send_task_assign(
        self,
        *,
        node_id: str,
        task_msg: JsonDict,
        fail_task: Callable[[str, str], Awaitable[None]],
    ) -> None:
        task_id = str(task_msg.get("task_id") or "")
        if not task_id:
            return
        try:
            await self.registry.send_json(node_id, task_msg)
        except Exception as e:
            await fail_task(task_id, f"ws send failed: {type(e).__name__}: {e}")
