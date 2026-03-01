from __future__ import annotations

from dataclasses import dataclass
from typing import Any


JsonDict = dict[str, Any]


@dataclass
class NodePresenceRecord:
    node_id: str
    first_seen_at: float
    last_seen_at: float
    online: bool = False
    last_online_at: float = 0.0
    last_offline_at: float = 0.0
    last_peer: str = ""
    host_name: str = ""
    online_count: int = 0
    offline_count: int = 0
    last_notify_at: float = 0.0
    pending_online_notify: bool = False


@dataclass
class TaskContext:
    task_id: str
    trace_id: str
    node_id: str
    thread_id: str
    chat_id: int
    placeholder_msg_id: int
    created_at: float
    session_key: str = ""
    result_mode: str = "send"
    last_progress_at: float = 0.0
    last_progress_seen_at: float = 0.0
    last_progress_text: str = ""
    pending_progress_text: str = ""
    last_progress_event: str = ""
    progress_lines: list[str] | None = None
    progress_last_message_at: float = 0.0


@dataclass
class ApprovalContext:
    approval_id: str
    node_id: str
    task_id: str
    trace_id: str
    rpc_id: int | None
    method: str
    params: JsonDict
    chat_id: int
    created_at: float
