#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CONTROL_HOSTPORT="${CODEX_MANAGER_CONTROL:-127.0.0.1:18766}"
CONTROL_TOKEN="${CODEX_MANAGER_CONTROL_TOKEN:-}"
NODE_ID="${1:-}"
TIMEOUT="${TIMEOUT:-30}"

if [[ -z "$NODE_ID" ]]; then
  cat <<EOF >&2
Usage:
  CODEX_MANAGER_CONTROL_TOKEN=... scripts/verify_phase2_appserver_rpc.sh <node_id>

Optional env:
  CODEX_MANAGER_CONTROL=127.0.0.1:18766
  TIMEOUT=30
EOF
  exit 2
fi

if [[ -z "$CONTROL_TOKEN" ]]; then
  echo "missing CODEX_MANAGER_CONTROL_TOKEN (must match manager control server token)" >&2
  exit 2
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "missing venv: run ./scripts/install.sh first" >&2
  exit 2
fi

. .venv/bin/activate

echo "phase=2.appserver_rpc node_id=$NODE_ID timeout=$TIMEOUT control=$CONTROL_HOSTPORT"

export CONTROL_HOSTPORT
export CONTROL_TOKEN
export NODE_ID
export TIMEOUT

python - <<'PY'
import json, os, socket, sys, time

hostport=os.environ["CONTROL_HOSTPORT"]
token=os.environ["CONTROL_TOKEN"]
node_id=os.environ["NODE_ID"]
timeout=float(os.environ.get("TIMEOUT","30"))
wait_online_s=float(os.environ.get("WAIT_ONLINE","30"))

host, port_s = hostport.rsplit(":", 1)
port = int(port_s)

def rpc(req: dict) -> dict:
  s=socket.create_connection((host, port), timeout=5.0)
  try:
    s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
    s.settimeout(timeout + 30.0)
    buf=b""
    while b"\n" not in buf:
      chunk=s.recv(65536)
      if not chunk:
        break
      buf += chunk
    line=buf.split(b"\n",1)[0].decode("utf-8","replace").strip()
    return json.loads(line) if line else {}
  finally:
    try: s.close()
    except Exception: pass

deadline=time.time() + max(1.0, wait_online_s)
while True:
  resp = rpc({"type":"servers","token":token})
  if not resp.get("ok"):
    print("FAIL servers:", resp, file=sys.stderr); raise SystemExit(2)
  online=resp.get("online") or []
  if node_id in online:
    break
  if time.time() >= deadline:
    print(f"FAIL node not online: {node_id}", file=sys.stderr); raise SystemExit(2)
  time.sleep(0.5)

def call(method: str, params: dict):
  t0=time.time()
  r=rpc({"type":"appserver","token":token,"node_id":node_id,"method":method,"params":params,"timeout":timeout})
  dt=int((time.time()-t0)*1000)
  if not r.get("ok"):
    raise RuntimeError(f"{method} control failed: {r!r}")
  inner=r.get("result") or {}
  if not isinstance(inner, dict) or not inner.get("ok"):
    raise RuntimeError(f"{method} failed: {inner!r}")
  print(f"method={method} ok=true latency_ms={dt}")
  return inner.get("result") or {}

call("thread/list", {"limit": 1})
call("model/list", {"limit": 3})
call("collaborationMode/list", {})
print("phase=2.appserver_rpc PASS")
PY
