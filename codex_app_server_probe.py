import argparse
import json

from codex_proxy_probe import main as _probe_main


def main() -> int:
    # Keep the old filename working; reuse the current probe implementation.
    return _probe_main()


if __name__ == "__main__":
    raise SystemExit(main())

