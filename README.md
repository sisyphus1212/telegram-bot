# Telegram Bot - Codex 集成

一个 Telegram 管理入口（manager）控制多台机器上的 Codex（proxy），支持一对多分发：

- **manager**：接收 Telegram 消息，选择一个在线 proxy 下发任务，收回结果后回写 Telegram
- **proxy**：运行在每台被控机器上，保持本机 `codex app-server` 常驻，通过 WS 与 manager 双向通信

详细架构见 [ARCHITECTURE.md](/root/telegram-bot/ARCHITECTURE.md)。

后续开发者接入指南见 [AGENTS.md](/root/telegram-bot/AGENTS.md)。

## 文件说明

- [ARCHITECTURE.md](/root/telegram-bot/ARCHITECTURE.md) - 架构与协议说明
- `codex_manager.py` - Manager（Telegram + WS registry + task dispatch）
- `telegram_bot.py` - 兼容入口（直接调用 `codex_manager.py`）
- `bot.py` - 兼容入口（直接调用 `codex_manager.py`，旧名字）
- `codex_proxy.py` - Proxy（WS client，内部使用本机 `codex app-server`）
- `codex_stdio_client.py` - Proxy 使用的 stdio JSON-RPC 客户端
- `codex_proxy_probe.py` - 自检脚本（验证本机 `codex app-server` 链路）
- `scripts/verify_phase1_probe.sh` - 阶段 1：proxy 本机 codex ping/pong 一键验证
- `scripts/verify_phase2_ws.sh` - 阶段 2：manager<->proxy WS 一键验证（需要开启 manager control server）
- `scripts/verify_phase2_appserver_rpc.sh` - 阶段 2：manager<->proxy app-server RPC 透传验证（需要开启 control server）
- `docs/verify_phase3_tg.md` - 阶段 3：TG 端到端验证说明
- `codex_app_server_client.py` / `codex_app_server_probe.py` - 历史兼容文件（转到新实现）
- `codex_config.example.json` - 配置示例（不要提交真实 token）
- `requirements.txt` - Python 依赖
- `scripts/install.sh` - 创建 venv 并安装依赖
- `scripts/run.sh` - 使用 venv 启动 manager
- `scripts/run_proxy.sh` - 使用 venv 启动 proxy
- `systemd/codex-manager.service` - systemd: manager 服务文件
- `systemd/codex-manager.env.example` - systemd: manager 环境变量示例
- `systemd/codex-proxy.service` - systemd: proxy 服务文件
- `systemd/codex-proxy.env.example` - systemd: proxy 环境变量示例
- `systemd/telegram-bot.service` - legacy: 旧服务名（仍可用，实际启动 manager）
- `systemd/telegram-bot.env.example` - legacy: 旧 env 示例（仍可用）
- `log/manager.log` - 运行日志（运行后生成）
- `sessions.json` - 会话存储文件（运行后生成）
  - v2 会保存 chat -> proxy 以及 per-proxy 的 current threadId
  - thread 内容由 Codex 自己保存在 `~/.codex/`，我们只保存“指针/路由”

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

Manager 的 Telegram token 推荐放 `codex_config.json`（不要提交，已在 `.gitignore`），也可以用环境变量 `TELEGRAM_BOT_TOKEN` 覆盖。

最小 `codex_config.json` 示例（只需要 Telegram token + WS 监听地址即可）：

```json
{
  "telegram_bot_token": "PUT-YOUR-TELEGRAM-BOT-TOKEN-HERE",
  "manager_ws_listen": "0.0.0.0:8765",
  "default_proxy": "proxy1"
}
```

本项目会在 `sessions.json` 中保存每个聊天选择的 `proxy_id` 以及 per-proxy 的 `current_thread_id`。

推荐用环境变量（systemd 也会用）：

- `TELEGRAM_BOT_TOKEN`: Telegram Bot token
- `CODEX_MANAGER_WS_LISTEN`: manager WS 监听地址（例如 `0.0.0.0:8765`）
- `CODEX_DEFAULT_PROXY`: 默认 proxy（可选）
- `CODEX_TASK_TIMEOUT`: 单次任务超时秒数（可选）
- `TELEGRAM_PROXY`: 如果你的环境需要代理访问 Telegram API（例如 `http://127.0.0.1:8080`）
- `CODEX_MANAGER_CONTROL_LISTEN`: 可选，本地 control server 监听地址（用于阶段 2 验证，例如 `127.0.0.1:18766`）
- `CODEX_MANAGER_CONTROL_TOKEN`: 可选，control server 必需 token（用于阶段 2 验证）

也可以在 `codex_config.json` 里显式配置 Telegram 代理（优先级低于 `TELEGRAM_PROXY`）：

```json
{
  "telegram_proxy": "http://127.0.0.1:8080"
}
```

安全相关（当前阶段可以先不设，但强烈建议后续补上）：

- `TELEGRAM_ALLOWED_USER_IDS`: 允许使用的 Telegram user id 列表(逗号分隔)，为空表示不限制

Proxy allowlist（可选，后续要收紧安全建议开启）也写在 `codex_config.json`：

```json
{
  "manager_ws_listen": "0.0.0.0:8765",
  "default_proxy": "proxy1",
  "proxies": {
    "proxy1": { "token": "REPLACE_ME" },
    "proxy2": { "token": "REPLACE_ME" }
  }
}
```

注意：当前代码处于“先打通链路”的 dev 模式，allowlist 目前只做告警不强制拦截（便于快速 bring-up）。

### 3. 配置（Proxy）

Proxy 运行在被控机器上，至少需要：

- `CODEX_MANAGER_WS`：manager 的 WS 地址，例如 `ws://MANAGER_IP:8765`
- `PROXY_ID`：本机 proxy 的名字（用于 `/use <proxy_id>`）
- `CODEX_CWD`：Codex 工作目录（默认当前目录）
- `CODEX_BIN`：`codex` 可执行文件（默认 `codex`）

可选：

- `PROXY_TOKEN`：与 allowlist 配合使用的 token（dev 模式可空）
- `PROXY_MAX_PENDING`：proxy 在 Codex 回答前允许挂起的最大任务数（默认 `10`）。超过会立刻回 `proxy queue full`。
- `CODEX_SANDBOX`：Codex sandbox（默认 `workspace-write`）。需要执行更高权限操作时可考虑 `danger-full-access`（风险极高）。
- `CODEX_APPROVAL_POLICY`：审批策略（不同 codex 版本枚举可能不同；本项目会尽量兼容官方文档值与本地实际值）。

> 说明：本项目的 `codex_stdio_client.py` 默认会对 app-server 的 `requestApproval` 请求返回 `decline`。因此当 `approval_policy=on-request` 时，某些命令/改文件会被 Codex 请求确认但被我们拒绝，从而表现为“权限问题”。要避免这一类失败，通常做法是把 `approval_policy` 设为 `never`，让 app-server 不再发起审批请求。

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

再启动 proxy（在每台需要执行 Codex 的机器上）：

推荐用 `proxy_config.json` 来配置，避免记环境变量。

```bash
. .venv/bin/activate
cp proxy_config.example.json proxy_config.json
editor proxy_config.json
python codex_proxy.py --config proxy_config.json
```

说明：

- 如果当前目录存在 `proxy_config.json`，`codex_proxy.py` 即使不带 `--config` 也会自动读取它。
- 优先级：命令行参数 > 环境变量 > JSON 配置 > 默认值。

### 6. 阶段化验证（强烈建议按顺序）

阶段 1：proxy 本机 codex（每台 proxy 机器都要做）

```bash
./scripts/verify_phase1_probe.sh --prompt ping --repeat 10 --timeout 60 --jsonl log/probe_phase1.jsonl
```

阶段 2：manager<->proxy WS（不经过 Telegram）

1. 在 manager 机器上启用 control server（建议只绑定 `127.0.0.1`）：

```bash
export CODEX_MANAGER_CONTROL_LISTEN=127.0.0.1:18766
export CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME
systemctl restart codex-manager.service
```

2. 在 manager 机器上对某个 proxy 做 WS 连通性验证（要求 proxy 已在线注册）：

```bash
export CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME
scripts/verify_phase2_ws.sh proxy27
```

可选：验证 app-server 透传（不经过 Telegram）：

```bash
export CODEX_MANAGER_CONTROL_TOKEN=REPLACE_ME
scripts/verify_phase2_appserver_rpc.sh proxy27
```

阶段 3：Telegram 端到端验证见 [docs/verify_phase3_tg.md](/root/telegram-bot/docs/verify_phase3_tg.md)。

### 7. 验证链路（TG）

在 Telegram 对话里：

1. `/proxy_list` 查看在线 proxy（旧命令 `/servers` 仍可用）
2. `/ping` 验证 Telegram -> manager -> Telegram（不经过 proxy）
3. `/proxy_use <proxy_id>` 选择一台机器（旧命令 `/use <proxy_id>` 仍可用）
4. 直接发一条消息，例如 `ping`
5. 预期会看到占位 `working...`，随后被编辑成 `[{proxy_id}] ...` 的结果或错误

### 7.1 Thread 管理命令（尽量对齐 app-server 官方 method）

- `/thread_start` -> `thread/start`（并设置当前聊天在当前 proxy 上的 `current_thread_id`）
- `/thread_resume threadId=<id>` -> `thread/resume`
- `/thread_list limit=5` -> `thread/list`
- `/thread_read threadId=<id>` -> `thread/read`
- `/thread_archive [threadId=<id>]` -> `thread/archive`
- `/thread_unarchive threadId=<id>` -> `thread/unarchive`
- `/thread_current` 显示当前聊天在当前 proxy 上保存的 threadId（这是客户端路由元数据，不是 app-server method）

### 8. 作为 Linux 服务（systemd）

Manager:

```bash
sudo cp systemd/codex-manager.service /etc/systemd/system/codex-manager.service
sudo cp systemd/codex-manager.env.example /etc/codex-manager.env
sudo editor /etc/codex-manager.env

sudo systemctl daemon-reload
sudo systemctl enable --now codex-manager.service

sudo systemctl status codex-manager.service
sudo journalctl -u codex-manager.service -f
```

Proxy:

```bash
sudo cp systemd/codex-proxy.service /etc/systemd/system/codex-proxy.service
sudo cp systemd/codex-proxy.env.example /etc/codex-proxy.env
sudo editor /etc/codex-proxy.env

sudo systemctl daemon-reload
sudo systemctl enable --now codex-proxy.service

sudo systemctl status codex-proxy.service
sudo journalctl -u codex-proxy.service -f
```

## 故障排除

1. Telegram 收不到消息：
   - 看 `journalctl -u codex-manager.service -f`
   - 很多环境需要设置 `TELEGRAM_PROXY`
   - 为了避免被系统 `HTTP_PROXY/HTTPS_PROXY` 环境变量干扰，本项目默认 `trust_env=false`，不会自动继承系统代理；需要的话请显式配置 `TELEGRAM_PROXY` 或 `codex_config.json` 的 `telegram_proxy`
2. Manager 看不到在线 proxy：
   - 看 `journalctl -u codex-proxy.service -f`
   - 确认 `CODEX_MANAGER_WS` 可达、端口放通
3. Codex 没回复或超时：
   - 在 proxy 机器上跑 `codex --version`
   - 先跑 `./scripts/verify_phase1_probe.sh --prompt ping --repeat 10` 确认本机链路
4. 阶段 2 验证脚本报 control server 不可用：
   - 确认 manager 启动时设置了 `CODEX_MANAGER_CONTROL_LISTEN` 和 `CODEX_MANAGER_CONTROL_TOKEN`
   - 确认 `scripts/verify_phase2_ws.sh` 使用同一个 token
5. 安全：
   - 真实接管 PC 前必须加 `TELEGRAM_ALLOWED_USER_IDS`，并启用 proxy allowlist + token 强校验

## 多机管理

- `/servers` 列出在线 proxy
- `/use <proxy_id>` 强制切换当前聊天使用的 proxy（会让下一次对话重置远端 thread）
- `/reset` 下次对话重置远端 thread
- `/pc on|off` 预留开关（当前最小协议未使用）
