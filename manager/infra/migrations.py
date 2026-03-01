from __future__ import annotations

from pathlib import Path
from typing import Any

from manager.infra.repo_sessions import SessionStore


def load_legacy_sessions_json(path: Path) -> dict[str, dict]:
    return {}


def migrate_legacy_sessions_if_needed(*, store: SessionStore, legacy_file: Path, logger: Any) -> None:
    # Legacy sessions.json migration has been removed.
    return
