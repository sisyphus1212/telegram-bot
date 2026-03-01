from __future__ import annotations

import time
from typing import Any


class SessionService:
    def __init__(self, sessions: dict[str, dict]) -> None:
        self.sessions = sessions

    def get_sess(self, sk: str) -> dict:
        sess = self.sessions.get(sk)
        if not isinstance(sess, dict):
            sess = {"node": "", "by_node": {}, "defaults": {}, "result_mode": "send"}
            self.sessions[sk] = sess
        if not isinstance(sess.get("by_node"), dict):
            sess["by_node"] = {}
        if not isinstance(sess.get("defaults"), dict):
            sess["defaults"] = {}
        if str(sess.get("result_mode") or "").strip() not in ("replace", "send"):
            sess["result_mode"] = "send"
        return sess

    def get_selected_node(self, sk: str) -> str:
        sess = self.get_sess(sk)
        return str(sess.get("node") or "").strip()

    def get_result_mode(self, sk: str) -> str:
        sess = self.get_sess(sk)
        mode = str(sess.get("result_mode") or "send").strip()
        aliases = {"replace": "replace", "edit": "replace", "send": "send", "separate": "send"}
        return aliases.get(mode, "send")

    def set_result_mode(self, sk: str, mode: str) -> None:
        mode = (mode or "").strip()
        aliases = {"replace": "replace", "edit": "replace", "send": "send", "separate": "send"}
        mode = aliases.get(mode, "send")
        sess = self.get_sess(sk)
        sess["result_mode"] = mode
        self.sessions[sk] = sess

    def set_selected_node(self, sk: str, node_id: str) -> None:
        sess = self.get_sess(sk)
        sess["node"] = str(node_id or "").strip()
        self.sessions[sk] = sess

    def get_current_thread_id(self, sk: str, node_id: str) -> str:
        sess = self.get_sess(sk)
        byp = sess["by_node"]
        entry = byp.get(node_id)
        if not isinstance(entry, dict):
            return ""
        return str(entry.get("current_thread_id") or "").strip()

    def get_defaults(self, sk: str) -> dict[str, Any]:
        sess = self.get_sess(sk)
        defaults = sess.get("defaults")
        return defaults if isinstance(defaults, dict) else {}

    def get_default_model(self, sk: str) -> str:
        defaults = self.get_defaults(sk)
        return str(defaults.get("model") or "").strip()

    def set_default_model(self, sk: str, model: str) -> None:
        sess = self.get_sess(sk)
        defaults = sess.get("defaults")
        if not isinstance(defaults, dict):
            defaults = {}
        model = str(model or "").strip()
        if model:
            defaults["model"] = model
        else:
            defaults.pop("model", None)
        sess["defaults"] = defaults
        self.sessions[sk] = sess

    def get_default_effort(self, sk: str) -> str:
        defaults = self.get_defaults(sk)
        effort = str(defaults.get("effort") or "").strip().lower()
        if effort in ("low", "medium", "high"):
            return effort
        return "medium"

    def set_default_effort(self, sk: str, effort: str) -> None:
        effort = str(effort or "").strip().lower()
        if effort not in ("low", "medium", "high"):
            effort = "medium"
        sess = self.get_sess(sk)
        defaults = sess.get("defaults")
        if not isinstance(defaults, dict):
            defaults = {}
        defaults["effort"] = effort
        sess["defaults"] = defaults
        self.sessions[sk] = sess

    def set_current_thread_id(self, sk: str, node_id: str, thread_id: str) -> None:
        sess = self.get_sess(sk)
        byp = sess["by_node"]
        entry = byp.get(node_id)
        if not isinstance(entry, dict):
            entry = {}
            byp[node_id] = entry
        entry["current_thread_id"] = str(thread_id or "")
        entry["last_used_at"] = int(time.time())
        sess["by_node"] = byp
        self.sessions[sk] = sess
