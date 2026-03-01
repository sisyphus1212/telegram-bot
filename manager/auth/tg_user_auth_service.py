from __future__ import annotations

import time
from pathlib import Path

from manager.infra.repo_tg_users import TgUserRepo


class TgUserAuthService:
    def __init__(self, db_path: Path, seed_users: set[int] | None = None) -> None:
        self._repo = TgUserRepo(db_path)
        if seed_users:
            self.seed_if_empty(seed_users)

    def seed_if_empty(self, users: set[int]) -> None:
        if self._repo.count() > 0:
            return
        now = int(time.time())
        for uid in sorted(users):
            self._repo.insert_ignore(user_id=int(uid), source="bootstrap", created_at=now)

    def is_allowed(self, user_id: int | None) -> bool:
        # Empty table means allow all (backward-compatible behavior).
        total = self._repo.count()
        if total == 0:
            return True
        if not isinstance(user_id, int):
            return False
        return self._repo.exists(int(user_id))
