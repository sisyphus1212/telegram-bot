from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets


async def run_ws_server(listen: str, registry: Any, core: Any, *, logger: Any) -> None:
    host, port_s = listen.rsplit(":", 1)
    port = int(port_s)

    async def handler(ws: Any):
        node_id = ""
        registered = False
        try:
            peer = getattr(ws, "remote_address", None)
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            msg = json.loads(raw)
            if not isinstance(msg, dict) or msg.get("type") != "register":
                await ws.send(json.dumps({"type": "register_error", "error": "expected register"}))
                return
            node_id = str(msg.get("node_id") or "").strip()
            token = str(msg.get("token") or "").strip()
            host_name = str(msg.get("host_name") or "").strip()
            sandbox = str(msg.get("sandbox") or "").strip()
            approval_policy = str(msg.get("approval_policy") or "").strip()
            if not node_id:
                await ws.send(json.dumps({"type": "register_error", "error": "missing node_id"}))
                return
            logger.info(f"op=ws.register peer={peer!r} node_id={node_id} host_name={host_name or '-'}")
            ok = await registry.register(
                node_id=node_id,
                token=token,
                ws=ws,
                host_name=host_name,
                peer=str(peer or ""),
                sandbox=sandbox,
                approval_policy=approval_policy,
            )
            if not ok:
                logger.warning(f"op=ws.register.reject peer={peer!r} node_id={node_id} host_name={host_name or '-'}")
                await ws.send(json.dumps({"type": "register_error", "error": "unauthorized"}))
                return
            registered = True
            await ws.send(json.dumps({"type": "register_ok"}))
            logger.info(f"node online: {node_id} host_name={host_name or '-'}")
            peer_s = ""
            if isinstance(peer, tuple) and len(peer) >= 2:
                peer_s = f"{peer[0]}:{peer[1]}"
            elif peer is not None:
                peer_s = str(peer)
            await core.on_node_online(node_id, peer=peer_s, host_name=host_name)

            async for raw in ws:
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(m, dict):
                    continue
                t = m.get("type")
                if t == "heartbeat":
                    await registry.heartbeat(node_id, ws)
                    continue
                if t in ("task_ack", "task_result", "task_progress", "appserver_response", "approval_request"):
                    await core.on_node_message(node_id, m)
                    continue
        except websockets.ConnectionClosed:
            return
        except Exception as e:
            logger.warning(f"ws handler error for node {node_id or '<unregistered>'}: {e}")
            return
        finally:
            if node_id and registered:
                removed = await registry.unregister_if_matches(node_id, ws)
                if removed:
                    logger.info(f"node offline: {node_id}")
                    try:
                        await core.on_node_offline(node_id)
                    except Exception:
                        pass

    async with websockets.serve(handler, host, port, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024):
        logger.info(f"WS listening on {listen}")
        await asyncio.Future()
