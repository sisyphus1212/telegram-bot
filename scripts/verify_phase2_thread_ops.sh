#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CONTROL_HOSTPORT="${CODEX_MANAGER_CONTROL:-127.0.0.1:18766}"
CONTROL_TOKEN="${CODEX_MANAGER_CONTROL_TOKEN:-}"
PROXY_ID="${1:-}"
THREAD_KEY="${THREAD_KEY:-probe:threadop}"
TIMEOUT="${TIMEOUT:-30}"

if [[ -z "$PROXY_ID" ]]; then
  cat <<EOF >&2
Usage:
  CODEX_MANAGER_CONTROL_TOKEN=... scripts/verify_phase2_thread_ops.sh <proxy_id>

Optional env:
  CODEX_MANAGER_CONTROL=127.0.0.1:18766
  THREAD_KEY=probe:threadop
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

echo "phase=2.thread_ops proxy_id=$PROXY_ID thread_key=$THREAD_KEY timeout=$TIMEOUT control=$CONTROL_HOSTPORT"

export CONTROL_HOSTPORT
export CONTROL_TOKEN
export PROXY_ID
export THREAD_KEY
export TIMEOUT

python - <<'PY'
import json, os, socket, sys

hostport=os.environ["CONTROL_HOSTPORT"]
token=os.environ["CONTROL_TOKEN"]
proxy_id=os.environ["PROXY_ID"]
thread_key=os.environ["THREAD_KEY"]
timeout=float(os.environ.get("TIMEOUT","30"))

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

def thread_op(op: str, index=None):
  req={"type":"thread_op","token":token,"proxy_id":proxy_id,"thread_key":thread_key,"op":op,"timeout":timeout}
  if index is not None:
    req["index"]=index
  r=rpc(req)
  if not r.get("ok"):
    raise RuntimeError(f"control failed: {r!r}")
  res=r.get("result") or {}
  if not isinstance(res, dict) or not res.get("ok"):
    raise RuntimeError(f"thread_op failed: {res!r}")
  return res

# list -> new -> list -> del(1) -> list
print("op=list", thread_op("list"))
print("op=new", thread_op("new"))
res=thread_op("list")
depth=int(res.get("depth") or 0)
if depth >= 1:
  print("op=del index=1", thread_op("del", 1))
else:
  print("skip del (depth=0)")
try:
  print("op=back", thread_op("back"))
except Exception as e:
  print("op=back expected-fail:", e)
print("phase=2.thread_ops PASS")
PY

