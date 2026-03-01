# Telegram Bot - Codex 集成

一个 Telegram 管理入口（manager）控制多台机器上的 Codex（node），支持一对多分发：

- **manager**：接收 Telegram 消息，选择一个在线 node 下发任务，收回结果后回写 Telegram
- **node**：运行在每台被控机器上，保持本机 `codex app-server` 常驻，通过 WS 与 manager 双向通信

详细架构见 [ARCHITECTURE.md](/root/telegram-bot/ARCHITECTURE.md)。

后续开发者接入指南见 [AGENTS.md](/root/telegram-bot/AGENTS.md)。

## 文档分工

- `README.md`：面向人类维护者，放安装、运行、测试、调试、部署、排障步骤。
- `AGENTS.md`：面向 Codex/AI 代理，放编码约束、结构导航、关键行为契约。

## 文件说明

- [ARCHITECTURE.md](/root/telegram-bot/ARCHITECTURE.md) - 架构与协议说明
- `codex_manager.py` - Manager（Telegram + WS registry + task dispatch）
- `codex_node.py` - Node 新入口（等价于 `codex_node.py`）
- `codex_node.py` - Node（WS client，内部使用本机 `codex app-server`）
- `bot_comm/stdio_client.py` - Node 使用的 stdio JSON-RPC 客户端
- `codex_node_probe.py` - 自检脚本（验证本机 `codex app-server` 链路）
- `scripts/verify_phase1_probe.sh` - 阶段 1：node 本机 codex ping/pong 一键验证
- `scripts/verify_phase2_ws.sh` - 阶段 2：manager<->node WS 一键验证（需要开启 manager control server）
- `scripts/verify_phase2_appserver_rpc.sh` - 阶段 2：manager<->node app-server RPC 透传验证（需要开启 control server）
- `docs/verify_phase3_tg.md` - 阶段 3：TG 端到端验证说明
- `manager_config.example.json` - manager 配置示例（复制为 `manager_config.json` 后填写；不要提交真实 token）
- `requirements.txt` - Python 依赖
- `scripts/install.sh` - 创建 venv 并安装依赖
- `scripts/run.sh` - 使用 venv 启动 manager
- `scripts/run_node.sh` - 使用 venv 启动 node（推荐）
- `scripts/run_node.sh` - 使用 venv 启动 node（脚本名兼容保留）
- `systemd/agent-manager.service` - systemd: manager 服务文件
- `systemd/agent-manager.env.example` - systemd: manager 环境变量示例
- `systemd/agent-node@.service` - systemd: node 多实例服务文件
- `systemd/agent-node.env.example` - systemd: node 代理环境变量示例（可选）
- `log/manager.log` - 运行日志（运行后生成）
- `manager_data.db` - manager 本地 SQLite 数据库（运行后生成）
  - 保存 chat -> node 路由、per-node current threadId、会话默认参数、node 鉴权 token、TG 用户授权表
  - thread 内容由 Codex 自己保存在 `~/.codex/`，本项目只保存“指针/路由”

## 依赖

- Linux
- Python 3.10+（本仓库已在 Python 3.12 上验证）
- 本机可执行的 `codex`（`codex --version`）

## 使用方法

### 1. 安装

```bash
./scripts/install.sh
```

### 2. 配置（Manager）

Manager 的 Telegram token 推荐放 `manager_config.json`（不要提交，已在 `.gitignore`），也可以用环境变量 `TELEGRAM_BOT_TOKEN` 覆盖。

最小 `manager_config.json` 示例（只需要 Telegram token + WS 监听地址即可）：

```json
{
  "telegram_bot_token": "PUT-YOUR-TELEGRAM-BOT-TOKEN-HERE",
  "manager_ws_listen": "0.0.0.0:8765",
  "default_node": "node1"
}
```

本项目会在 `manager_data.db` 中保存每个聊天选择的 `node_id`（历史字段名兼容保留）、per-node 的 `current_thread_id`，以及当前会话偏好的 `model`（用于在每次 `turn/start` 下发 per-turn override；按 app-server 语义会写回 thread 默认值）。

推荐用环境变量（systemd 也会用）：

- `TELEGRAM_BOT_TOKEN`: Telegram Bot token
- `CODEX_MANAGER_WS_LISTEN`: manager WS 监听地址（例如 `0.0.0.0:8765`）
- `CODEX_DEFAULT_NODE`: 默认 node（可选，环境变量名历史兼容保留）
- `CODEX_TASK_TIMEOUT`: 单次任务超时秒数（可选）
- 系统代理：如果你的环境需要代理访问 Telegram API，请使用系统环境变量 `HTTP_PROXY/HTTPS_PROXY/NO_PROXY`
- `CODEX_MANAGER_CONTROL_LISTEN`: 可选，本地 control server 监听地址（用于阶段 2 验证，例如 `127.0.0.1:18766`）
- `CODEX_MANAGER_CONTROL_TOKEN`: 可选，control server 必需 token（用于阶段 2 验证）
- `TELEGRAM_STARTUP_NOTIFY_CHAT_IDS`: 可选，manager 启动后向这些 chat_id 发送一条启动汇报（逗号/空格分隔）
- `manager_data.db`：manager 本地 SQLite 数据库（自动创建/维护），用于 manager 持久化状态（含 node 鉴权 token）

不再支持在 `manager_config.json` 或 `TELEGRAM_PROXY` 单独配置 Telegram 代理，避免出现“多处配置互相打架”导致的不可用问题。
注意：systemd 服务不会读取 `~/.bashrc`，如果你把代理 export 在 `~/.bashrc`，需要把 `HTTP_PROXY/HTTPS_PROXY/NO_PROXY` 同步进 systemd 的 `EnvironmentFile`（见下文 `scripts/sync_proxy_to_agent_env.sh`）。

安全相关（当前阶段可以先不设，但强烈建议后续补上）：

- `TELEGRAM_ALLOWED_USER_IDS`: 允许使用的 Telegram user id 列表(逗号分隔)，为空表示不限制

Node 鉴权 token 由 TG 管理命令维护：

- `/token generate [node=<node_id>] [note=<text>]` 生成 token
- `/token list [revoked=true|false]` 查询 token
- `/token revoke <token_id>` 废除 token

说明：token 是唯一鉴权依据；无需 `CODEX_ENFORCE_NODE_ALLOWLIST`。

### 3. 配置（Node）

Node 运行在被控机器上（每台机器一个进程），至少需要：

- `CODEX_MANAGER_WS`：manager 的 WS 地址，例如 `ws://MANAGER_IP:8765`
- `NODE_ID`：本机 node 的名字
- `CODEX_CWD`：Codex 工作目录（默认当前目录）
- `CODEX_BIN`：`codex` 可执行文件（默认 `codex`）

可选：

- `NODE_TOKEN`：node 注册到 manager 时使用的鉴权 token
- `NODE_MAX_PENDING`：node 在 Codex 回答前允许挂起的最大任务数（默认 `10`）。超过会立刻回 `queue full`。
- `CODEX_SANDBOX`：Codex sandbox（默认 `workspace-write`）。需要执行更高权限操作时可考虑 `danger-full-access`（风险极高）。
- `CODEX_APPROVAL_POLICY`：审批策略（不同 codex 版本枚举可能不同；本项目会尽量兼容官方文档值与本地实际值）。

> 说明：当前实现已经支持把 app-server 的审批请求转发到 Telegram。`approval_policy=onRequest` 时，只有当 Codex 主动发起 `requestApproval`，Telegram 才会收到 `/approve <approval_id>`、`/approve session <approval_id>`、`/decline <approval_id>` 这类审批命令。

### 4. 自检

先单独验证某台机器上的 `codex app-server` 链路是否正常：

```bash
./scripts/verify_phase1_probe.sh --prompt ping --repeat 10 --timeout 60
```

### 5. 启动（前台）

先启动 manager：

```bash
./scripts/run.sh
```

再启动 node（在每台需要执行 Codex 的机器上）：

推荐用 `node_config.json` 来配置，避免记环境变量。

```bash
. .venv/bin/activate
cp node_config.example.json node_config.json
editor node_config.json
python codex_node.py --config node_config.json
```

说明：

- 如果当前目录存在 `node_config.json`，`codex_node.py` 即使不带 `--config` 也会自动读取它。
- 优先级：命令行参数 > 环境变量 > JSON 配置 > 默认值。

`node_config.example.json` 是一个 JSONC 模板（支持注释），里面已经写好了 `sandbox` / `approval_policy` 的“注释开关”示例：

- `sandbox`：
  - 常用：`workspaceWrite`（允许在工作区写）
  - 高危：`dangerFullAccess`（几乎全开）
- `approval_policy`：
  - `onRequest`：按 app-server 原生审批机制处理，需要时会把审批转发到 Telegram
  - `never`：不走审批（高危，但能避免“权限确认导致失败”）

### 6. 阶段化验证（强烈建议按顺序）

阶段 1：node 本机 codex（每台 node 机器都要做）

```bash
./scripts/verify_phase1_probe.sh --prompt ping --repeat 10 --timeout 60 --jsonl log/probe_phase1.jsonl
```

阶段 2：manager<->node WS（不经过 Telegram）

1. 在 manager 机器上启用 control server（建议只绑定 `127.0.0.1`）：

```bash
export CODEX_MANAGER_CONTROL_LISTEN=127.0.0.1:18766
export CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME
systemctl restart agent-manager.service
```

2. 在 manager 机器上对某个 node 做 WS 连通性验证（要求 node 已在线注册）：

```bash
export CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME
scripts/verify_phase2_ws.sh node27
```

可选：验证 app-server 透传（不经过 Telegram）：

```bash
export CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME
scripts/verify_phase2_appserver_rpc.sh node27
```

阶段 3：Telegram 端到端验证见 [docs/verify_phase3_tg.md](/root/telegram-bot/docs/verify_phase3_tg.md)。

### 7. 验证链路（TG）

在 Telegram 对话里：

1. `/node` 打开 node 选择面板
2. `/ping` 验证 Telegram -> manager -> Telegram（不经过 node）
3. 点击按钮选择一台机器
4. 可选：`/model <model_id>` 设置当前会话模型（每次 `turn/start` 都会带上该 model）
5. 可选：`/model effort <low|medium|high>` 设置 reasoning effort（默认 `medium`，每次 `turn/start` 都会带上该 effort）
6. 可选：`/result replace` 或 `/result send`
7. 直接发一条消息，例如 `ping`
7. 预期会先看到占位 `working...`
8. 若任务执行较久，占位消息会被周期性编辑，并以“增量日志”的方式保留已发生的进展（默认约 5 秒最多更新一次；会自动去掉重复噪音，并在过长时折叠中间步骤）
9. 最终结果输出：
   - `replace`：结果覆盖 placeholder
   - `send`：placeholder 保留为 done/failed，结果单独发新消息

### 7.1 Thread 管理命令（尽量对齐 app-server 官方 method）

- `/thread start` -> `thread/start`（并设置当前聊天在当前 node 上的 `current_thread_id`）
- `/thread resume threadId=<id>` -> `thread/resume`
- `/thread list limit=5` -> `thread/list`
- `/thread read threadId=<id>` -> `thread/read`
- `/thread archive [threadId=<id>]` -> `thread/archive`
- `/thread unarchive threadId=<id>` -> `thread/unarchive`
- `/thread current` 显示当前聊天在当前 node 上保存的 threadId（这是客户端路由元数据，不是 app-server method）

### 8. 作为 Linux 服务（systemd）

Manager:

```bash
sudo cp systemd/agent-manager.service /etc/systemd/system/agent-manager.service
sudo cp systemd/agent-manager.env.example /etc/agent-manager.env
sudo editor /etc/agent-manager.env

sudo systemctl daemon-reload
sudo systemctl enable --now agent-manager.service

sudo systemctl status agent-manager.service
sudo journalctl -u agent-manager.service -f
```

Node:

```bash
sudo cp systemd/agent-node@.service /etc/systemd/system/agent-node@.service
sudo cp systemd/agent-node.env.example /etc/agent-node.env
sudo editor /etc/agent-node.env

# 每个实例读取自己的配置文件：
#   /root/telegram-bot/node_config.<instance>.json
# 例如 node_config.1.json / node_config.2.json
#
# 如果你希望直接复用目标机当前登录 shell 的代理变量（而不是手写 /etc/agent-node.env），可以：
#   CODEX_AGENT_MANAGER_ENV_FILE=/etc/agent-node.env sudo -E /root/telegram-bot/scripts/sync_proxy_to_agent_env.sh

sudo systemctl daemon-reload
sudo systemctl enable --now agent-node@1.service
# 多实例：
# sudo systemctl enable --now agent-node@2.service

sudo systemctl status agent-node@1.service
sudo journalctl -u agent-node@1.service -f
```

## 故障排除

1. Telegram 收不到消息：
   - 看 `journalctl -u agent-manager.service -f`
   - 很多环境需要设置系统 `HTTP_PROXY/HTTPS_PROXY`
   - 代理策略：只继承系统 `HTTP_PROXY/HTTPS_PROXY/NO_PROXY`
   - 如果你在 `~/.bashrc` 里 export 了代理：systemd 不会自动继承，需要执行 `sudo -E /root/telegram-bot/scripts/sync_proxy_to_agent_env.sh` 后重启 `agent-manager.service`
   - 如果环境里有 `ALL_PROXY/all_proxy`（尤其是 socks5）：建议清空它（本项目启动脚本会 `unset ALL_PROXY all_proxy`，避免 python-telegram-bot/httpx 走 SOCKS 导致报错）
2. Manager 看不到在线 node：
   - 看 `journalctl -u agent-node@1.service -f`（多实例分别看）
   - 确认 `CODEX_MANAGER_WS` 可达、端口放通
3. Codex 没回复或超时：
   - 在 node 机器上跑 `codex --version`
   - 先跑 `./scripts/verify_phase1_probe.sh --prompt ping --repeat 10` 确认本机链路
4. 阶段 2 验证脚本报 control server 不可用：
   - 确认 manager 启动时设置了 `CODEX_MANAGER_CONTROL_LISTEN` 和 `CODEX_MANAGER_CONTROL_TOKEN`
   - 确认 `scripts/verify_phase2_ws.sh` 使用同一个 token
5. 安全：
   - 真实接管 PC 前必须加 `TELEGRAM_ALLOWED_USER_IDS`，并只给可信节点分发 token

## 多机管理

- `/node` 弹出在线 node 按钮，并显示 current
