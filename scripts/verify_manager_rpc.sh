#!/usr/bin/env bash
set -euo pipefail

HOSTPORT="${CODEX_MANAGER_CONTROL:-127.0.0.1:18766}"
TOKEN="${CODEX_MANAGER_CONTROL_TOKEN:-}"
PY="${PYTHON_BIN:-python3}"
RPC="${RPC_SCRIPT:-scripts/node_manager_rpc.py}"
TEST_NODE_ID="${TEST_NODE_ID:-rpc_verify_$(date +%s)}"

if [[ -z "$TOKEN" ]]; then
  echo "missing CODEX_MANAGER_CONTROL_TOKEN" >&2
  exit 2
fi
if [[ ! -f "$RPC" ]]; then
  echo "rpc script not found: $RPC" >&2
  exit 2
fi

run_rpc() {
  local action="$1"
  local params_json="${2-}"
  if [[ -z "$params_json" ]]; then
    params_json='{}'
  fi
  "$PY" "$RPC" "$action" --token "$TOKEN" --hostport "$HOSTPORT" --params-json "$params_json" --print-json
}

echo "[1/6] system.status"
status_json="$(run_rpc system.status '{}')"
"$PY" - "$status_json" <<'PY'
import json,sys
j=json.loads(sys.argv[1])
assert j.get("ok") is True
st=((j.get("result") or {}).get("status") or {})
assert "draining" in st and "inflight_tasks" in st
print("ok")
PY

echo "[2/6] system.servers"
servers_json="$(run_rpc system.servers '{}')"
"$PY" - "$servers_json" <<'PY'
import json,sys
j=json.loads(sys.argv[1])
assert j.get("ok") is True
res=j.get("result") or {}
assert isinstance(res.get("online"), list)
print("ok")
PY

echo "[3/6] token.generate node_id=$TEST_NODE_ID"
gen_json="$(run_rpc token.generate "{\"node_id\":\"$TEST_NODE_ID\",\"note\":\"verify_manager_rpc\"}")"
"$PY" - "$gen_json" <<'PY'
import json,sys
j=json.loads(sys.argv[1])
assert j.get("ok") is True
r=j.get("result") or {}
assert r.get("token_id")
assert r.get("token","").startswith("nt_")
cfg=r.get("node_config") or {}
assert cfg.get("node_id")==r.get("node_id")
assert cfg.get("node_token")==r.get("token")
print("ok")
PY
TOKEN_ID="$(echo "$gen_json" | "$PY" - <<'PY'
"$PY" - "$gen_json" <<'PY'
import json,sys
j=json.loads(sys.argv[1])
print((j.get("result") or {}).get("token_id") or "")
PY
)"
if [[ -z "$TOKEN_ID" ]]; then
  echo "failed to parse token_id" >&2
  exit 1
fi

echo "[4/6] token.list include_revoked=false"
list_json="$(run_rpc token.list '{"include_revoked":false}')"
 "$PY" - "$list_json" "$TOKEN_ID" <<'PY'
import json,sys
token_id=sys.argv[2]
j=json.loads(sys.argv[1])
assert j.get("ok") is True
items=(j.get("result") or {}).get("items") or []
assert any((it.get("token_id") == token_id) for it in items)
print("ok")
PY

echo "[5/6] token.revoke token_id=$TOKEN_ID"
rev_json="$(run_rpc token.revoke "{\"token_id\":\"$TOKEN_ID\"}")"
 "$PY" - "$rev_json" "$TOKEN_ID" <<'PY'
import json,sys
token_id=sys.argv[2]
j=json.loads(sys.argv[1])
assert j.get("ok") is True
r=j.get("result") or {}
assert r.get("token_id")==token_id
assert r.get("ok") is True
print("ok")
PY

echo "[6/6] token.list include_revoked=true"
list2_json="$(run_rpc token.list '{"include_revoked":true}')"
 "$PY" - "$list2_json" "$TOKEN_ID" <<'PY'
import json,sys
token_id=sys.argv[2]
j=json.loads(sys.argv[1])
assert j.get("ok") is True
items=(j.get("result") or {}).get("items") or []
hit=[it for it in items if it.get("token_id")==token_id]
assert hit, "token not found in include_revoked list"
print("ok")
PY

echo "PASS verify_manager_rpc token_id=$TOKEN_ID node_id=$TEST_NODE_ID hostport=$HOSTPORT"
