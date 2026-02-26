"""
Legacy entrypoint kept for compatibility.

The project moved to WS-based proxy registration. The manager process is `codex_manager.py`.
"""

from codex_manager import main


if __name__ == "__main__":
    raise SystemExit(main())

