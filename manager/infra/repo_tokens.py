from __future__ import annotations

import sqlite3
from pathlib import Path


class NodeTokenRepo:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS node_tokens (
              token_id TEXT PRIMARY KEY,
              token_hash TEXT NOT NULL UNIQUE,
              node_id TEXT NOT NULL DEFAULT '',
              note TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              created_by INTEGER,
              revoked_at INTEGER,
              revoked_by INTEGER
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_node_tokens_active_hash ON node_tokens(token_hash, revoked_at)")
        self._conn.commit()

    def insert_ignore(self, *, token_id: str, token_hash: str, node_id: str, note: str, created_at: int) -> bool:
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO node_tokens(token_id, token_hash, node_id, note, created_at, created_by, revoked_at, revoked_by) VALUES(?,?,?,?,?,NULL,NULL,NULL)",
            (token_id, token_hash, node_id, note, created_at),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def upsert_active(self, *, token_id: str, token_hash: str, node_id: str, note: str, created_at: int, created_by: int | None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO node_tokens(token_id, token_hash, node_id, note, created_at, created_by, revoked_at, revoked_by) VALUES(?,?,?,?,?,?,NULL,NULL)",
            (token_id, token_hash, node_id, note, created_at, created_by),
        )
        self._conn.commit()

    def list_items(self, *, include_revoked: bool) -> list[dict]:
        q = "SELECT token_id, token_hash, node_id, note, created_at, created_by, revoked_at, revoked_by FROM node_tokens"
        if not include_revoked:
            q += " WHERE revoked_at IS NULL"
        q += " ORDER BY created_at DESC"
        return [dict(r) for r in self._conn.execute(q).fetchall()]

    def get_active_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(1) AS n FROM node_tokens WHERE revoked_at IS NULL").fetchone()
        return int(row["n"] if row else 0)

    def find_by_token_hash(self, token_hash: str) -> dict | None:
        row = self._conn.execute("SELECT token_id, node_id, revoked_at FROM node_tokens WHERE token_hash=?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def find_by_id(self, token_id: str) -> dict | None:
        row = self._conn.execute("SELECT token_id, revoked_at FROM node_tokens WHERE token_id=?", (token_id,)).fetchone()
        return dict(row) if row else None

    def mark_revoked(self, *, token_id: str, revoked_at: int, revoked_by: int | None) -> None:
        self._conn.execute("UPDATE node_tokens SET revoked_at=?, revoked_by=? WHERE token_id=?", (revoked_at, revoked_by, token_id))
        self._conn.commit()
