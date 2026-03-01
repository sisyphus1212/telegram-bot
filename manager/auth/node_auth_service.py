from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path

from manager.infra.repo_tokens import NodeTokenRepo


class NodeAuthService:
    def __init__(self, db_path: Path) -> None:
        self._repo = NodeTokenRepo(db_path)

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _token_id(token_hash: str) -> str:
        return token_hash[:12]

    def active_count(self) -> int:
        return self._repo.get_active_count()

    async def generate(self, *, node_id: str, note: str, created_by: int | None) -> dict[str, Any]:
        token = f"nt_{secrets.token_urlsafe(24)}"
        token_hash = self._hash_token(token)
        token_id = self._token_id(token_hash)
        now = int(time.time())
        entry = {
            "token_id": token_id,
            "token_hash": token_hash,
            "node_id": str(node_id or "").strip(),
            "note": str(note or "").strip(),
            "created_at": now,
            "created_by": int(created_by) if isinstance(created_by, int) else None,
            "revoked_at": None,
            "revoked_by": None,
        }
        self._repo.upsert_active(
            token_id=entry["token_id"],
            token_hash=entry["token_hash"],
            node_id=entry["node_id"],
            note=entry["note"],
            created_at=entry["created_at"],
            created_by=entry["created_by"],
        )
        return {"token": token, "token_id": token_id, "entry": entry}

    async def list_items(self, *, include_revoked: bool = True) -> list[dict[str, Any]]:
        return self._repo.list_items(include_revoked=include_revoked)

    async def revoke(self, *, token_id: str, revoked_by: int | None) -> tuple[bool, str]:
        now = int(time.time())
        row = self._repo.find_by_id(token_id)
        if row is None:
            return False, "token_id not found"
        if row.get("revoked_at") is not None:
            return False, "already revoked"
        self._repo.mark_revoked(token_id=token_id, revoked_at=now, revoked_by=int(revoked_by) if isinstance(revoked_by, int) else None)
        return True, "ok"

    async def validate_for_node(self, *, node_id: str, token: str) -> tuple[bool, str]:
        token = str(token or "").strip()
        if not token:
            return False, "missing token"
        token_hash = self._hash_token(token)
        row = self._repo.find_by_token_hash(token_hash)
        if row is None:
            return False, "unknown token"
        if row.get("revoked_at") is not None:
            return False, "revoked"
        bind_id = str(row.get("node_id") or "").strip()
        if bind_id and bind_id != node_id:
            return False, f"token bound to {bind_id}"
        return True, "ok"
