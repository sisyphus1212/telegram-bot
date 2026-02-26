import json
import sys
import tempfile
from pathlib import Path


def _load_sessions(path: Path) -> dict:
    data = json.loads(path.read_text("utf-8"))
    if isinstance(data, dict) and data.get("version") == 2:
        return data
    return {"version": 0, "sessions": data}


def main() -> int:
    # Ensure repo root importable when running from scripts/.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    # Legacy shape: { session_key: {proxy, pc_mode, reset_next} }
    legacy = {"tg:1:2": {"proxy": "proxy1", "pc_mode": False, "reset_next": True}}
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sessions.json"
        p.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), "utf-8")

        # Import manager's migration logic by executing load/save functions on a temp path.
        import codex_manager as m

        orig = m.SESSIONS_FILE
        try:
            m.SESSIONS_FILE = p
            sessions = m.load_sessions()
            assert isinstance(sessions, dict) and "tg:1:2" in sessions
            m.save_sessions(sessions)
            v2 = _load_sessions(p)
            assert v2.get("version") == 2
            assert isinstance(v2.get("sessions"), dict) and "tg:1:2" in v2["sessions"]
        finally:
            m.SESSIONS_FILE = orig

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
