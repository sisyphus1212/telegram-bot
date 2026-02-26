import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from codex_stdio_client import CodexAppServerStdioProcess, CodexLocalAppServerConfig


def _kv(**items: object) -> str:
    parts: list[str] = []
    for k, v in items.items():
        if v is None:
            continue
        s = str(v)
        # keep it greppable
        s = s.replace("\n", "\\n")
        parts.append(f"{k}={s}")
    return " ".join(parts)


async def _run(prompt: str, cwd: str, codex_bin: str, *, timeout_s: float) -> dict:
    app = CodexAppServerStdioProcess(CodexLocalAppServerConfig(codex_bin=codex_bin, cwd=cwd))
    await app.start()
    await app.ensure_started_and_initialized(client_name="codex_proxy_probe", version="0.0")
    tid = await app.thread_start(cwd=cwd, sandbox="workspace-write", approval_policy="on-request", personality="pragmatic")
    turn_id = await app.turn_start_text(thread_id=tid, text=prompt)
    text = await app.run_turn_and_collect_agent_message(thread_id=tid, turn_id=turn_id, timeout_s=float(timeout_s))
    await app.stop()
    return {"ok": True, "threadId": tid, "turnId": turn_id, "text": (text or "").strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="ping")
    ap.add_argument("--cwd", default=os.environ.get("CODEX_CWD") or str(Path(__file__).resolve().parent))
    ap.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN") or "codex")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--repeat", type=int, default=1, help="repeat N times; exit non-zero if any failed")
    ap.add_argument("--interval-ms", type=int, default=0, help="sleep between runs")
    ap.add_argument("--jsonl", default="", help="optional jsonl output path (one line per run)")
    ap.add_argument("--json", action="store_true", help="print single-run JSON result (for backward compatibility)")
    args = ap.parse_args()

    repeat = max(1, int(args.repeat))
    interval_ms = max(0, int(args.interval_ms))
    jsonl_path = args.jsonl.strip()
    timeout_s = float(args.timeout)

    ok_count = 0
    latencies: list[float] = []
    any_fail = False

    f_jsonl = None
    if jsonl_path:
        Path(jsonl_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        f_jsonl = open(jsonl_path, "a", encoding="utf-8")

    try:
        for i in range(1, repeat + 1):
            trace_id = uuid.uuid4().hex
            t0 = time.time()
            print(_kv(op="probe.start", trace_id=trace_id, i=i, repeat=repeat, timeout_s=timeout_s, prompt_len=len(args.prompt)))
            try:
                out = asyncio.run(_run(args.prompt, cwd=args.cwd, codex_bin=args.codex_bin, timeout_s=timeout_s))
                latency_ms = (time.time() - t0) * 1000.0
                latencies.append(latency_ms)
                ok = bool(out.get("ok"))
                ok_count += 1 if ok else 0
                any_fail = any_fail or (not ok)
                print(_kv(op="probe.done", trace_id=trace_id, ok=ok, latency_ms=int(latency_ms), text_len=len(str(out.get("text") or ""))))
                if f_jsonl is not None:
                    line = {
                        "op": "probe.done",
                        "trace_id": trace_id,
                        "i": i,
                        "repeat": repeat,
                        "ok": ok,
                        "latency_ms": latency_ms,
                        "out": out,
                    }
                    f_jsonl.write(json.dumps(line, ensure_ascii=False) + "\n")
                    f_jsonl.flush()
                if args.json and repeat == 1:
                    print(json.dumps(out, ensure_ascii=False, indent=2))
            except Exception as e:
                latency_ms = (time.time() - t0) * 1000.0
                latencies.append(latency_ms)
                any_fail = True
                print(_kv(op="probe.done", trace_id=trace_id, ok=False, latency_ms=int(latency_ms), error=f"{type(e).__name__}: {e}"))
                if f_jsonl is not None:
                    line = {
                        "op": "probe.done",
                        "trace_id": trace_id,
                        "i": i,
                        "repeat": repeat,
                        "ok": False,
                        "latency_ms": latency_ms,
                        "error": f"{type(e).__name__}: {e}",
                    }
                    f_jsonl.write(json.dumps(line, ensure_ascii=False) + "\n")
                    f_jsonl.flush()
            if i != repeat and interval_ms:
                time.sleep(interval_ms / 1000.0)
    finally:
        if f_jsonl is not None:
            f_jsonl.close()

    # Summary
    lat_sorted = sorted(latencies)
    p50 = lat_sorted[int(0.50 * (len(lat_sorted) - 1))] if lat_sorted else 0.0
    p95 = lat_sorted[int(0.95 * (len(lat_sorted) - 1))] if lat_sorted else 0.0
    print(_kv(op="probe.summary", ok=(not any_fail), ok_count=ok_count, total=repeat, p50_ms=int(p50), p95_ms=int(p95)))
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
