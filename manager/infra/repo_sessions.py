from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
              session_key TEXT PRIMARY KEY,
              selected_node_id TEXT NOT NULL DEFAULT '',
              result_mode TEXT NOT NULL DEFAULT 'send',
              defaults_json TEXT NOT NULL DEFAULT '{}',
              updated_at INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_session_threads (
              session_key TEXT NOT NULL,
              node_id TEXT NOT NULL,
              current_thread_id TEXT NOT NULL DEFAULT '',
              last_used_at INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (session_key, node_id)
            )
            """
        )
        self._conn.commit()

    def load_all(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        rows = self._conn.execute(
            "SELECT session_key, selected_node_id, result_mode, defaults_json FROM chat_sessions"
        ).fetchall()
        for r in rows:
            sk = str(r["session_key"])
            try:
                defaults = json.loads(str(r["defaults_json"] or "{}"))
            except Exception:
                defaults = {}
            if not isinstance(defaults, dict):
                defaults = {}
            out[sk] = {
                "node": str(r["selected_node_id"] or ""),
                "result_mode": str(r["result_mode"] or "send"),
                "defaults": defaults,
                "by_node": {},
            }

        trows = self._conn.execute(
            "SELECT session_key, node_id, current_thread_id, last_used_at FROM chat_session_threads"
        ).fetchall()
        for r in trows:
            sk = str(r["session_key"])
            node_id = str(r["node_id"])
            sess = out.setdefault(sk, {"node": "", "result_mode": "send", "defaults": {}, "by_node": {}})
            byn = sess.get("by_node") if isinstance(sess.get("by_node"), dict) else {}
            byn[node_id] = {
                "current_thread_id": str(r["current_thread_id"] or ""),
                "last_used_at": int(r["last_used_at"] or 0),
            }
            sess["by_node"] = byn
            out[sk] = sess
        return out

    def save_all(self, sessions: dict[str, dict[str, Any]]) -> None:
        now = int(time.time())
        cur = self._conn.cursor()
        cur.execute("DELETE FROM chat_session_threads")
        cur.execute("DELETE FROM chat_sessions")
        for sk, sess in sessions.items():
            if not isinstance(sk, str) or not isinstance(sess, dict):
                continue
            selected = str(sess.get("node") or "")
            mode = str(sess.get("result_mode") or "send")
            if mode not in ("send", "replace"):
                mode = "send"
            defaults = sess.get("defaults") if isinstance(sess.get("defaults"), dict) else {}
            cur.execute(
                "INSERT INTO chat_sessions(session_key, selected_node_id, result_mode, defaults_json, updated_at) VALUES(?,?,?,?,?)",
                (sk, selected, mode, json.dumps(defaults, ensure_ascii=False), now),
            )
            byn = sess.get("by_node") if isinstance(sess.get("by_node"), dict) else {}
            for node_id, entry in byn.items():
                if not isinstance(node_id, str) or not isinstance(entry, dict):
                    continue
                cur.execute(
                    "INSERT INTO chat_session_threads(session_key, node_id, current_thread_id, last_used_at) VALUES(?,?,?,?)",
                    (
                        sk,
                        node_id,
                        str(entry.get("current_thread_id") or ""),
                        int(entry.get("last_used_at") or 0),
                    ),
                )
        self._conn.commit()
