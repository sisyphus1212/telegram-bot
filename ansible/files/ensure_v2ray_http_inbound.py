#!/usr/bin/env python3
"""
Ensure a v2ray config has an HTTP inbound on 0.0.0.0:<port>.

Why:
- Some environments need a local HTTP proxy for reaching chatgpt.com (Codex) and/or Telegram.
- v2ray configs are often JSONC (with // or /* */ comments). We do minimal stripping.

Usage:
  python3 ensure_v2ray_http_inbound.py /etc/v2ray/config.json 8080
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _strip_jsonc(raw: str) -> str:
    out: list[str] = []
    i = 0
    n = len(raw)
    in_str = False
    esc = False
    while i < n:
        c = raw[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue

        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue

        if c == "/" and i + 1 < n and raw[i + 1] == "/":
            i += 2
            while i < n and raw[i] not in "\r\n":
                i += 1
            continue

        if c == "/" and i + 1 < n and raw[i + 1] == "*":
            i += 2
            while i + 1 < n and not (raw[i] == "*" and raw[i + 1] == "/"):
                i += 1
            i += 2
            continue

        out.append(c)
        i += 1
    return "".join(out)


def _ensure_http_inbound(cfg: dict[str, Any], port: int) -> bool:
    inbounds = cfg.get("inbounds")
    if not isinstance(inbounds, list):
        inbounds = []
        cfg["inbounds"] = inbounds

    def is_http_inbound(x: Any) -> bool:
        if not isinstance(x, dict):
            return False
        if str(x.get("protocol") or "").lower() != "http":
            return False
        try:
            p = int(x.get("port"))
        except Exception:
            return False
        return p == port

    changed = False
    found = False
    for ib in inbounds:
        if is_http_inbound(ib):
            found = True
            if ib.get("listen") != "0.0.0.0":
                ib["listen"] = "0.0.0.0"
                changed = True
            ib.setdefault("settings", {})
            ib.setdefault("tag", f"http-{port}")

    if not found:
        inbounds.append(
            {
                "listen": "0.0.0.0",
                "port": port,
                "protocol": "http",
                "settings": {"timeout": 0},
                "tag": f"http-{port}",
            }
        )
        changed = True

    return changed


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: ensure_v2ray_http_inbound.py <config_path> <port>", file=sys.stderr)
        return 2

    path = sys.argv[1]
    port = int(sys.argv[2])

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    data = json.loads(_strip_jsonc(raw))
    if not isinstance(data, dict):
        raise TypeError("v2ray config root must be an object")

    changed = _ensure_http_inbound(data, port)

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")

    print("changed" if changed else "noop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

