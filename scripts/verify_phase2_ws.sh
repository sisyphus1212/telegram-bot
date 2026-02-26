#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CONTROL_HOSTPORT="${CODEX_MANAGER_CONTROL:-127.0.0.1:18766}"
CONTROL_TOKEN="${CODEX_MANAGER_CONTROL_TOKEN:-}"
PROXY_ID="${1:-}"
PROMPT="${PROMPT:-ping}"
REPEAT="${REPEAT:-10}"
TIMEOUT="${TIMEOUT:-60}"

if [[ -z "$PROXY_ID" ]]; then
  cat <<EOF >&2
Usage:
  CODEX_MANAGER_CONTROL_TOKEN=... scripts/verify_phase2_ws.sh <proxy_id>

Optional env:
  CODEX_MANAGER_CONTROL=127.0.0.1:18766
  PROMPT=ping
  REPEAT=10
  TIMEOUT=60
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

echo "phase=2 proxy_id=$PROXY_ID repeat=$REPEAT timeout=$TIMEOUT control=$CONTROL_HOSTPORT"

export CONTROL_HOSTPORT
export CONTROL_TOKEN
export PROXY_ID
export PROMPT
export REPEAT
export TIMEOUT

# Wait control server up (use bash /dev/tcp, no extra deps).
host="${CONTROL_HOSTPORT%:*}"
port="${CONTROL_HOSTPORT##*:}"
for _ in $(seq 1 50); do
  if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
    exec 3<&-
    exec 3>&-
    break
  fi
  sleep 0.1
done

python - <<'PY'
import json, os, socket, sys, time

hostport=os.environ.get("CONTROL_HOSTPORT")
token=os.environ.get("CONTROL_TOKEN")
proxy_id=os.environ.get("PROXY_ID")
prompt=os.environ.get("PROMPT","ping")
repeat=int(os.environ.get("REPEAT","10"))
timeout=float(os.environ.get("TIMEOUT","60"))

host, port_s = hostport.rsplit(":", 1)
port = int(port_s)

def rpc(req: dict) -> dict:
  s=socket.create_connection((host, port), timeout=5.0)
  try:
    s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
    buf=b""
    # Control 端会同步等待 codex 返回，所以这里要等到 timeout_s + buffer。
    wait_s = timeout + 30.0
    t0=time.time()
    while b"\n" not in buf:
      if (time.time() - t0) > wait_s:
        raise TimeoutError("control rpc timeout")
      chunk=s.recv(65536)
      if not chunk:
        break
      buf += chunk
    line=buf.split(b"\n",1)[0].decode("utf-8","replace").strip()
    return json.loads(line) if line else {}
  finally:
    try: s.close()
    except Exception: pass

resp = rpc({"type":"servers","token":token})
if not resp.get("ok"):
  print("phase=2 FAIL control servers:", resp, file=sys.stderr)
  raise SystemExit(2)
online=resp.get("online") or []
print("servers online:", online)
if proxy_id not in online:
  print(f"phase=2 FAIL proxy not online: {proxy_id}", file=sys.stderr)
  raise SystemExit(2)

ok=0
lat=[]
for i in range(1, repeat+1):
  t0=time.time()
  r = rpc({"type":"dispatch","token":token,"proxy_id":proxy_id,"prompt":prompt,"timeout":timeout})
  dt=(time.time()-t0)*1000.0
  lat.append(dt)
  if r.get("ok") and isinstance(r.get("result"), dict) and r["result"].get("ok"):
    ok += 1
    print(f"i={i} ok=true latency_ms={int(dt)}")
  else:
    print(f"i={i} ok=false latency_ms={int(dt)} resp={r!r}", file=sys.stderr)
    raise SystemExit(1)

lat.sort()
p50=lat[int(0.50*(len(lat)-1))] if lat else 0.0
p95=lat[int(0.95*(len(lat)-1))] if lat else 0.0
print(f"phase=2 PASS ok_count={ok} total={repeat} p50_ms={int(p50)} p95_ms={int(p95)}")
PY
