#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
from typing import Any


def _readline_json(sock: socket.socket) -> dict[str, Any]:
    fp = sock.makefile("r", encoding="utf-8", newline="\n")
    line = fp.readline()
    if not line:
        raise RuntimeError("empty response from manager control")
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise RuntimeError("invalid response: not object")
    return obj


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Unified RPC client for manager control server (type=rpc)."
    )
    ap.add_argument("action", help="RPC action, e.g. token.generate / system.status")
    ap.add_argument(
        "--params-json",
        default="{}",
        help="JSON object for params, e.g. '{\"node_id\":\"master\"}'",
    )
    ap.add_argument("--hostport", default=os.environ.get("CODEX_MANAGER_CONTROL", "127.0.0.1:18766"))
    ap.add_argument("--token", default=os.environ.get("CODEX_MANAGER_CONTROL_TOKEN", ""))
    ap.add_argument("--timeout", type=float, default=float(os.environ.get("CODEX_MANAGER_CONTROL_TIMEOUT", "30") or "30"))
    ap.add_argument("--print-json", action="store_true", help="Print full response envelope")
    args = ap.parse_args()

    if not args.token:
        print("missing token: set --token or CODEX_MANAGER_CONTROL_TOKEN", file=sys.stderr)
        return 2

    try:
        host, port_s = args.hostport.rsplit(":", 1)
        port = int(port_s)
    except Exception:
        print(f"invalid --hostport: {args.hostport!r}", file=sys.stderr)
        return 2

    try:
        params = json.loads(args.params_json)
    except Exception as e:
        print(f"--params-json invalid json: {e}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("--params-json must be a JSON object", file=sys.stderr)
        return 2

    req: dict[str, Any] = {
        "type": "rpc",
        "id": uuid.uuid4().hex[:16],
        "token": args.token,
        "action": args.action.strip(),
        "params": params,
    }

    try:
        with socket.create_connection((host, port), timeout=args.timeout) as s:
            s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            resp = _readline_json(s)
    except Exception as e:
        print(f"rpc transport error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.print_json:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return 0 if bool(resp.get("ok")) else 1

    if bool(resp.get("ok")):
        print(json.dumps(resp.get("result", {}), ensure_ascii=False, indent=2))
        return 0

    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
