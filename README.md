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

本项目还会在 `sessions.json` 中保存每个聊天选择的 `proxy_id`（Codex thread 由 proxy 端维护）。

推荐用环境变量（systemd 也会用）：

- `TELEGRAM_BOT_TOKEN`: Telegram Bot token
- `CODEX_MANAGER_WS_LISTEN`: manager WS 监听地址（例如 `0.0.0.0:8765`）
- `CODEX_DEFAULT_PROXY`: 默认 proxy（可选）
- `CODEX_TASK_TIMEOUT`: 单次任务超时秒数（可选）
- `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`: 如果你的环境需要代理访问 Telegram API

也可以在 `codex_config.json` 里显式配置 Telegram 代理（优先级低于环境变量）：

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

### 4. 自检

先单独验证某台机器上的 `codex app-server` 链路是否正常：

```bash
. .venv/bin/activate
python codex_proxy_probe.py --prompt ping
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

### 6. 验证链路

在 Telegram 对话里：

1. `/servers` 查看在线 proxy
2. `/ping` 验证 Telegram -> manager -> Telegram（不经过 proxy）
2. 直接发一条消息，例如 `ping`
3. 预期会看到 `pong` 或 Codex 的回复

### 7. 作为 Linux 服务（systemd）

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
   - 很多环境需要设置 `HTTP_PROXY/HTTPS_PROXY`
   - 本项目会优先使用 `HTTPS_PROXY/HTTP_PROXY`（或 `codex_config.json` 里的 `telegram_proxy`）显式配置到 Telegram HTTP 客户端
2. Manager 看不到在线 proxy：
   - 看 `journalctl -u codex-proxy.service -f`
   - 确认 `CODEX_MANAGER_WS` 可达、端口放通
3. Codex 没回复或超时：
   - 在 proxy 机器上跑 `codex --version`
   - 先跑 `python codex_proxy_probe.py --prompt ping` 确认本机链路
4. 安全：
   - 真实接管 PC 前必须加 `TELEGRAM_ALLOWED_USER_IDS`，并启用 proxy allowlist + token 强校验

## 多机管理

- `/servers` 列出在线 proxy
- `/use <proxy_id>` 强制切换当前聊天使用的 proxy（会让下一次对话重置远端 thread）
- `/reset` 下次对话重置远端 thread
- `/pc on|off` 预留开关（当前最小协议未使用）
