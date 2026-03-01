# manager-control-cli

## 目的

在 manager 本机通过本地控制口（control server）直接发控制请求，尤其用于：

- 生成 node token
- 直接拿到可落地的 `node_config.json`
- 查询在线节点与控制面状态

## 前置条件

manager 已启用 control server：

- `CODEX_MANAGER_CONTROL_LISTEN`（例如 `127.0.0.1:18766`）
- `CODEX_MANAGER_CONTROL_TOKEN`（调用鉴权）

## 推荐命令

1. 基础连通性

```bash
CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME scripts/manager_ctl.sh status
CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME scripts/manager_ctl.sh servers
```

统一 RPC（推荐给自动化脚本）：

```bash
python3 scripts/node_manager_rpc.py system.status --token "$CODEX_MANAGER_CONTROL_TOKEN"
python3 scripts/node_manager_rpc.py system.servers --token "$CODEX_MANAGER_CONTROL_TOKEN"
```

2. 生成 token（完整返回）

```bash
CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME scripts/manager_ctl.sh token-generate --node-id my_node --note "bootstrap"
```

3. 仅输出 node_config.json（用于直接部署）

```bash
CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME scripts/manager_ctl.sh token-generate --node-id my_node --print-config
```

或走统一 RPC 包络：

```bash
python3 scripts/node_manager_rpc.py token.generate \
  --token "$CODEX_MANAGER_CONTROL_TOKEN" \
  --params-json '{"node_id":"my_node","note":"bootstrap"}' \
  --print-json
```

4. 查询 / 废除 token

```bash
CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME scripts/manager_ctl.sh token-list
CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME scripts/manager_ctl.sh token-revoke --token-id <token_id>
```

## 约束

- 该流程是“本地脚本 <-> manager control server”，不依赖 Telegram 命令链路。
- 默认不把 token 写入文件；需要落地时由调用者自行重定向。
- 生产环境建议 control server 只绑定 `127.0.0.1`。

## 测试用例（必须执行）

回归脚本（推荐）：

```bash
CODEX_MANAGER_CONTROL=127.0.0.1:18766 \
CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME \
scripts/verify_manager_rpc.sh
```

覆盖用例矩阵：

1. `system.status`：控制面可达，返回 `draining/inflight_tasks`。
2. `system.servers`：在线节点列表可读。
3. `token.generate`：返回 `token_id/token/node_config`，且 `node_config.node_token == token`。
4. `token.list(include_revoked=false)`：新 token 可查询。
5. `token.revoke`：可废除指定 token。
6. `token.list(include_revoked=true)`：被废除 token 仍可在全量列表找到。
