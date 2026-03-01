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
    node_auth: Any,
    session_store: SessionStore,
    base_dir: Path,
    manager_public_ws: str,
    logger: Any,
) -> None:
    """
    Local control plane for phase-2 verification.
    JSON-lines over TCP, default bind should be 127.0.0.1 only.
    """
    host, port_s = listen.rsplit(":", 1)
    port = int(port_s)

    def _build_node_config(node_id: str, plain_token: str) -> dict[str, Any]:
        return {
            "manager_ws": manager_public_ws,
            "node_id": node_id,
            "node_token": plain_token,
            "max_pending": 10,
            "sandbox": "dangerFullAccess",
            "approval_policy": "onRequest",
            "codex_cwd": str(base_dir),
            "codex_bin": "codex",
        }

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
                if t == "rpc":
                    req_id = str(req.get("id") or "")
                    action = str(req.get("action") or "").strip()
                    params = req.get("params") if isinstance(req.get("params"), dict) else {}

                    def rpc_ok(result: dict[str, Any]) -> dict[str, Any]:
                        return {"ok": True, "type": "rpc", "id": req_id, "action": action, "result": result}

                    def rpc_err(code: str, message: str) -> dict[str, Any]:
                        return {"ok": False, "type": "rpc", "id": req_id, "action": action, "error": {"code": code, "message": message}}

                    try:
                        if action == "system.status":
                            try:
                                status = await core.get_runtime_status()
                            except Exception:
                                status = {"draining": bool(core.is_draining()), "inflight_tasks": -1, "pending_approvals": -1}
                            out = rpc_ok({"status": status})
                            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                            await writer.drain()
                            continue
                        if action == "system.servers":
                            try:
                                details = await registry.get_online_snapshot()
                            except Exception:
                                details = []
                            out = rpc_ok(
                                {
                                    "online": registry.online_node_ids(),
                                    "allowed": registry.allowed_node_ids(),
                                    "details": details,
                                }
                            )
                            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                            await writer.drain()
                            continue
                        if action == "task.dispatch":
                            node_id = str(params.get("node_id") or "").strip()
                            prompt = str(params.get("prompt") or "ping")
                            timeout_s = float(params.get("timeout") or 60.0)
                            model = str(params.get("model") or "").strip()
                            effort = str(params.get("effort") or "").strip()
                            if not node_id:
                                out = rpc_err("bad_request", "missing node_id")
                            else:
                                try:
                                    res = await core.dispatch_once(node_id, prompt, timeout_s=timeout_s, model=model, effort=effort)
                                    out = rpc_ok({"dispatch": res})
                                except Exception as e:
                                    out = rpc_ok({"dispatch": {"ok": False, "error": f"{type(e).__name__}: {e}"}})
                            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                            await writer.drain()
                            continue
                        if action == "appserver.call":
                            node_id = str(params.get("node_id") or "").strip()
                            method = str(params.get("method") or "").strip()
                            method_params = params.get("params")
                            timeout_s = float(params.get("timeout") or 30.0)
                            if not node_id:
                                out = rpc_err("bad_request", "missing node_id")
                            elif not method:
                                out = rpc_err("bad_request", "missing method")
                            else:
                                try:
                                    res = await core.appserver_call(node_id, method, method_params if isinstance(method_params, dict) else {}, timeout_s=timeout_s)
                                    out = rpc_ok({"appserver": res})
                                except Exception as e:
                                    out = rpc_ok({"appserver": {"ok": False, "error": f"{type(e).__name__}: {e}"}})
                            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                            await writer.drain()
                            continue
                        if action == "task.dispatch_text":
                            node_id = str(params.get("node_id") or "").strip()
                            session_key = str(params.get("session_key") or "").strip()
                            text = str(params.get("text") or "")
                            timeout_s = float(params.get("timeout") or 60.0)
                            if not node_id or not session_key:
                                out = rpc_err("bad_request", "missing node_id/session_key")
                            else:
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
                                    out = rpc_ok({"thread_id": thread_id, "dispatch_text": res})
                                except Exception as e:
                                    out = rpc_ok({"dispatch_text": {"ok": False, "error": f"{type(e).__name__}: {e}"}})
                            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                            await writer.drain()
                            continue
                        if action == "token.generate":
                            node_id = str(params.get("node_id") or "").strip()
                            note = str(params.get("note") or "").strip()
                            created_by_raw = params.get("created_by")
                            created_by = int(created_by_raw) if isinstance(created_by_raw, int) else None
                            if not node_id:
                                out = rpc_err("bad_request", "missing node_id")
                            else:
                                rec = await node_auth.generate(node_id=node_id, note=note, created_by=created_by)
                                plain_token = str(rec.get("token") or "")
                                token_id = str(rec.get("token_id") or "")
                                out = rpc_ok(
                                    {
                                        "token_id": token_id,
                                        "node_id": node_id,
                                        "note": note,
                                        "token": plain_token,
                                        "node_config": _build_node_config(node_id, plain_token),
                                    }
                                )
                            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                            await writer.drain()
                            continue
                        if action == "token.list":
                            include_revoked = bool(params.get("include_revoked"))
                            items = await node_auth.list_items(include_revoked=include_revoked)
                            out = rpc_ok({"items": items})
                            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                            await writer.drain()
                            continue
                        if action == "token.revoke":
                            token_id = str(params.get("token_id") or "").strip()
                            revoked_by_raw = params.get("revoked_by")
                            revoked_by = int(revoked_by_raw) if isinstance(revoked_by_raw, int) else None
                            if not token_id:
                                out = rpc_err("bad_request", "missing token_id")
                            else:
                                ok, reason = await node_auth.revoke(token_id=token_id, revoked_by=revoked_by)
                                out = rpc_ok({"ok": bool(ok), "token_id": token_id, "reason": reason})
                            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                            await writer.drain()
                            continue
                        if action == "node.meta.set":
                            node_id = str(params.get("node_id") or "").strip()
                            if not node_id:
                                out = rpc_err("bad_request", "missing node_id")
                            else:
                                ok = await registry.set_node_capabilities(node_id=node_id, capabilities=params.get("capabilities"))
                                if not ok:
                                    out = rpc_err("not_found", f"node offline: {node_id}")
                                else:
                                    out = rpc_ok(
                                        {
                                            "node_id": node_id,
                                            "capabilities": registry.get_node_capabilities(node_id),
                                        }
                                    )
                            writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                            await writer.drain()
                            continue
                        out = rpc_err("bad_action", f"unknown action: {action}")
                        writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    except Exception as e:
                        out = rpc_err("internal_error", f"{type(e).__name__}: {e}")
                        writer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
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
                if t == "token_generate":
                    node_id = str(req.get("node_id") or "").strip()
                    note = str(req.get("note") or "").strip()
                    created_by_raw = req.get("created_by")
                    created_by = int(created_by_raw) if isinstance(created_by_raw, int) else None
                    if not node_id:
                        writer.write((json.dumps({"ok": False, "type": "token_generate", "error": "missing node_id"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    try:
                        rec = await node_auth.generate(node_id=node_id, note=note, created_by=created_by)
                        plain_token = str(rec.get("token") or "")
                        token_id = str(rec.get("token_id") or "")
                        resp = {
                            "ok": True,
                            "type": "token_generate",
                            "token_id": token_id,
                            "node_id": node_id,
                            "note": note,
                            "token": plain_token,
                            "node_config": _build_node_config(node_id, plain_token),
                        }
                        writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=token_generate ok=true node_id={node_id} token_id={token_id}")
                    except Exception as e:
                        writer.write((json.dumps({"ok": False, "type": "token_generate", "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=token_generate ok=false node_id={node_id} error={type(e).__name__}")
                    continue
                if t == "token_list":
                    include_revoked = bool(req.get("include_revoked"))
                    try:
                        items = await node_auth.list_items(include_revoked=include_revoked)
                        writer.write((json.dumps({"ok": True, "type": "token_list", "items": items}, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=token_list ok=true include_revoked={include_revoked} count={len(items)}")
                    except Exception as e:
                        writer.write((json.dumps({"ok": False, "type": "token_list", "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=token_list ok=false error={type(e).__name__}")
                    continue
                if t == "token_revoke":
                    token_id = str(req.get("token_id") or "").strip()
                    revoked_by_raw = req.get("revoked_by")
                    revoked_by = int(revoked_by_raw) if isinstance(revoked_by_raw, int) else None
                    if not token_id:
                        writer.write((json.dumps({"ok": False, "type": "token_revoke", "error": "missing token_id"}) + "\n").encode("utf-8"))
                        await writer.drain()
                        continue
                    try:
                        ok, reason = await node_auth.revoke(token_id=token_id, revoked_by=revoked_by)
                        writer.write((json.dumps({"ok": bool(ok), "type": "token_revoke", "token_id": token_id, "reason": reason}, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=token_revoke ok={bool(ok)} token_id={token_id}")
                    except Exception as e:
                        writer.write((json.dumps({"ok": False, "type": "token_revoke", "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n").encode("utf-8"))
                        await writer.drain()
                        logger.info(f"op=ctl.send peer={peer} type=token_revoke ok=false token_id={token_id} error={type(e).__name__}")
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
