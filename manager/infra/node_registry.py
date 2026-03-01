from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from manager.auth.node_auth_service import NodeAuthService


JsonDict = dict[str, Any]


@dataclass
class NodeConn:
    node_id: str
    ws: Any
    last_seen: float
    send_lock: asyncio.Lock
    host_name: str = ""
    peer: str = ""
    connected_at: float = 0.0
    sandbox: str = ""
    approval_policy: str = ""


class NodeRegistry:
    def __init__(self, node_auth: NodeAuthService, *, logger: Any) -> None:
        self._node_auth = node_auth
        self._logger = logger
        self._lock = asyncio.Lock()
        self._conns: dict[str, NodeConn] = {}

    def allowed_node_ids(self) -> list[str]:
        return []

    def online_node_ids(self) -> list[str]:
        return sorted(self._conns.keys())

    def is_online(self, node_id: str) -> bool:
        return node_id in self._conns

    async def register(
        self,
        node_id: str,
        token: str,
        ws: Any,
        *,
        host_name: str = "",
        peer: str = "",
        sandbox: str = "",
        approval_policy: str = "",
    ) -> bool:
        ok, reason = await self._node_auth.validate_for_node(node_id=node_id, token=token)
        if not ok:
            self._logger.warning(f"node_id {node_id!r} token invalid; rejecting: {reason}")
            return False
        async with self._lock:
            now = time.time()
            self._conns[node_id] = NodeConn(
                node_id=node_id,
                ws=ws,
                last_seen=now,
                send_lock=asyncio.Lock(),
                host_name=str(host_name or "").strip(),
                peer=str(peer or "").strip(),
                connected_at=now,
                sandbox=str(sandbox or "").strip(),
                approval_policy=str(approval_policy or "").strip(),
            )
        return True

    async def unregister_if_matches(self, node_id: str, ws: Any) -> bool:
        async with self._lock:
            cur = self._conns.get(node_id)
            if not cur or cur.ws is not ws:
                return False
            self._conns.pop(node_id, None)
            return True

    async def heartbeat(self, node_id: str, ws: Any) -> None:
        async with self._lock:
            cur = self._conns.get(node_id)
            if cur and cur.ws is ws:
                cur.last_seen = time.time()

    async def get_online_snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            items = [
                {
                    "node_id": c.node_id,
                    "host_name": c.host_name,
                    "peer": c.peer,
                    "connected_at": c.connected_at,
                    "last_seen": c.last_seen,
                    "sandbox": c.sandbox,
                    "approval_policy": c.approval_policy,
                }
                for c in self._conns.values()
            ]
        items.sort(key=lambda x: str(x.get("node_id") or ""))
        return items

    def get_runtime_defaults(self, node_id: str) -> tuple[str, str]:
        c = self._conns.get(node_id)
        if not c:
            return "", ""
        return (str(c.sandbox or "").strip(), str(c.approval_policy or "").strip())

    def get_node_label(self, node_id: str) -> str:
        c = self._conns.get(node_id)
        if not c:
            return node_id
        hn = (c.host_name or "").strip()
        return f"{node_id} ({hn})" if hn else node_id

    async def send_json(self, node_id: str, msg: JsonDict) -> None:
        async with self._lock:
            conn = self._conns.get(node_id)
            if not conn:
                raise RuntimeError(f"node offline: {node_id}")
            ws = conn.ws
            lock = conn.send_lock
        payload = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        trace_id = str(msg.get("trace_id") or "")
        task_id = str(msg.get("task_id") or "")
        async with lock:
            self._logger.info(
                f"op=ws.send node_id={node_id} type={msg.get('type')} trace_id={trace_id} task_id={task_id} bytes={len(payload)}"
            )
            await asyncio.wait_for(ws.send(payload), timeout=5.0)
            self._logger.info(f"op=ws.send.done node_id={node_id} type={msg.get('type')} trace_id={trace_id} task_id={task_id}")
