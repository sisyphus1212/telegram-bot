from __future__ import annotations

import sqlite3
from pathlib import Path


class TgUserRepo:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tg_allowed_users (
              user_id INTEGER PRIMARY KEY,
              source TEXT NOT NULL DEFAULT 'manual',
              created_at INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(1) AS n FROM tg_allowed_users").fetchone()
        return int(row["n"] if row else 0)

    def insert_ignore(self, *, user_id: int, source: str, created_at: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO tg_allowed_users(user_id, source, created_at) VALUES(?,?,?)",
            (user_id, source, created_at),
        )
        self._conn.commit()

    def exists(self, user_id: int) -> bool:
        row = self._conn.execute("SELECT 1 FROM tg_allowed_users WHERE user_id=?", (user_id,)).fetchone()
        return row is not None
