from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from manager.infra.repo_sessions import SessionStore


async def run_control_server(
    listen: str,
    token: str,
    *,
    registry: Any,
    core: Any,
    session_store: SessionStore,
    base_dir: Path,
    logger: Any,
) -> None:
    """
    Local control plane for phase-2 verification.
    JSON-lines over TCP, default bind should be 127.0.0.1 only.
    """
    host, port_s = listen.rsplit(":", 1)
    port = int(port_s)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except Exception:
                    writer.write((json.dumps({"ok": False, "error": "bad json"}) + "\n").encode("utf-8"))
                    await writer.drain()
                    continue
                if not isinstance(req, dict):
                    writer.write((json.dumps({"ok": False, "error": "bad request"}) + "\n").encode("utf-8"))
                    await writer.drain()
                    continue
                if str(req.get("token") or "") != token:
                    logger.warning(f"op=ctl.recv ok=false peer={peer} error=unauthorized")
                    writer.write((json.dumps({"ok": False, "error": "unauthorized"}) + "\n").encode("utf-8"))
                    await writer.drain()
                    continue
                t = str(req.get("type") or "")
                if t in ("dispatch", "appserver", "dispatch_text"):
                    try:
                        if bool(core.is_draining()):
                            writer.write((json.dumps({"ok": False, "type": t, "error": "manager draining"}) + "\n").encode("utf-8"))
                            await writer.drain()
                            continue
                    except Exception:
                        pass
                if t == "status":
                    try:
                        status = await core.get_runtime_status()
                    except Exception:
                        status = {"draining": bool(core.is_draining()), "inflight_tasks": -1, "pending_approvals": -1}
                    resp = {"ok": True, "type": "status", "status": status}
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    await writer.drain()
                    logger.info(
                        "op=ctl.send peer=%s type=status ok=true draining=%s inflight=%s approvals=%s",
                        peer,
                        status.get("draining"),
                        status.get("inflight_tasks"),
                        status.get("pending_approvals"),
                    )
                    continue
                if t == "servers":
                    details: list[dict[str, Any]] = []
                    try:
                        details = await registry.get_online_snapshot()
                    except Exception:
                        details = []
                    resp = {
                        "ok": True,
                        "type": "servers",
                        "online": registry.online_node_ids(),
                        "allowed": registry.allowed_node_ids(),
                        "details": details,
                    }
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                    await writer.drain()
                    logger.info(f"op=ctl.send peer={peer} type=servers ok=true")
                    continue
                if t == "dispatch":
                    node_id = str(req.get("node_id") or "").strip()
                    prompt = str(req.get("prompt") or "ping")
                    timeout_s = float(req.get("timeout") or 60.0)
                    model = str(req.get("model") or "").strip()
                    effort = str(req.get("effort") or "").strip()
                    if not node_id:
                        writer.write((json.dumps({"ok": False, "type": "dispatch", "error": "missing node_id"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    try:
                        res = await core.dispatch_once(node_id, prompt, timeout_s=timeout_s, model=model, effort=effort)
                        resp = {"ok": True, "type": "dispatch", "result": res}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=dispatch ok=true node_id={node_id}")
                    except Exception as e:
                        resp = {"ok": True, "type": "dispatch", "result": {"ok": False, "error": f"{type(e).__name__}: {e}"}}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=dispatch ok=true node_id={node_id} result_ok=false error={type(e).__name__}")
                    continue
                if t == "appserver":
                    node_id = str(req.get("node_id") or "").strip()
                    method = str(req.get("method") or "").strip()
                    params = req.get("params")
                    timeout_s = float(req.get("timeout") or 30.0)
                    if not node_id:
                        writer.write((json.dumps({"ok": False, "type": "appserver", "error": "missing node_id"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    if not method:
                        writer.write((json.dumps({"ok": False, "type": "appserver", "error": "missing method"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    try:
                        res = await core.appserver_call(node_id, method, params if isinstance(params, dict) else {}, timeout_s=timeout_s)
                        resp = {"ok": True, "type": "appserver", "result": res}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=appserver ok=true node_id={node_id} method={method}")
                    except Exception as e:
                        resp = {"ok": True, "type": "appserver", "result": {"ok": False, "error": f"{type(e).__name__}: {e}"}}
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=appserver ok=true node_id={node_id} method={method} result_ok=false error={type(e).__name__}")
                    continue
                if t == "dispatch_text":
                    node_id = str(req.get("node_id") or "").strip()
                    session_key = str(req.get("session_key") or "").strip()
                    text = str(req.get("text") or "")
                    timeout_s = float(req.get("timeout") or 60.0)
                    if not node_id or not session_key:
                        writer.write((json.dumps({"ok": False, "type": "dispatch_text", "error": "missing node_id/session_key"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    try:
                        sessions = session_store.load_all()
                        sess = sessions.get(session_key) or {"node": node_id, "by_node": {}, "defaults": {}}
                        byp = sess.get("by_node") if isinstance(sess.get("by_node"), dict) else {}
                        entry = byp.get(node_id) if isinstance(byp.get(node_id), dict) else {}
                        thread_id = str(entry.get("current_thread_id") or "")
                        if not thread_id:
                            rep = await core.appserver_call(
                                node_id,
                                "thread/start",
                                {"cwd": str(base_dir), "personality": "pragmatic"},
                                timeout_s=min(timeout_s, 60.0),
                            )
                            if not bool(rep.get("ok")):
                                raise RuntimeError(f"thread/start failed: {rep.get('error')}")
                            result = rep.get("result") if isinstance(rep.get("result"), dict) else {}
                            thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
                            thread_id = str(thread.get("id") or "")
                            if not thread_id:
                                raise RuntimeError(f"thread/start missing id: {result!r}")
                            byp.setdefault(node_id, {})["current_thread_id"] = thread_id
                            sess["by_node"] = byp
                            sessions[session_key] = sess
                            session_store.save_all(sessions)
                        res = await core.dispatch_task(node_id, thread_id, text, thread_key=session_key, timeout_s=timeout_s)
                        writer.write((json.dumps({"ok": True, "type": "dispatch_text", "thread_id": thread_id, "result": res}, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=dispatch_text ok=true node_id={node_id} session_key={session_key}")
                    except Exception as e:
                        writer.write((json.dumps({"ok": True, "type": "dispatch_text", "result": {"ok": False, "error": f"{type(e).__name__}: {e}"}}, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=dispatch_text ok=true node_id={node_id} result_ok=false error={type(e).__name__}")
                    continue
                writer.write((json.dumps({"ok": False, "error": "unknown type"}) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(_handle, host=host, port=port)
    logger.info(f"Control listening on {listen}")
    async with server:
        await server.serve_forever()
