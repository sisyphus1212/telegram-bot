# 开发说明

本项目包含一个 Codex Manager（Telegram + WS 控制面）和多个 Codex Proxy（WS 客户端）。Manager 把 Telegram 消息转发给某个在线 Proxy；每个 Proxy 管理本机的 `codex app-server`（stdio JSON-RPC），执行任务并把结果回传给 Manager。

建议先阅读 [ARCHITECTURE.md](/root/telegram-bot/ARCHITECTURE.md)。

## 快速检查

- 本机可用：`codex --version`
- 本机链路自检（不依赖 WS/TG）：`python codex_proxy_probe.py --prompt ping`

## Local Run

## 本地运行（含 Telegram）

```bash
./scripts/install.sh
export TELEGRAM_BOT_TOKEN=...
./scripts/run.sh
```

## 本地 WS 冒烟测试（不启动 Telegram）

终端 1（manager）：

```bash
. .venv/bin/activate
python codex_manager.py --ws-only
```

终端 2（proxy）：

```bash
. .venv/bin/activate
export CODEX_MANAGER_WS=ws://127.0.0.1:8765
export PROXY_ID=proxy1
export PROXY_TOKEN=
python codex_proxy.py
```

终端 3（派发一次任务并退出）：

```bash
. .venv/bin/activate
python codex_manager.py --ws-only --dispatch-proxy proxy1 --prompt ping --timeout 60
```

## WS Protocol (v0)

## WS 协议（v0）

- `register` -> `register_ok`
- `task_assign` -> `task_result`
- 可选：`heartbeat`

具体 JSON 结构见 [ARCHITECTURE.md](/root/telegram-bot/ARCHITECTURE.md)。

## Security

## 安全

- 当前阶段为了快速打通链路，代码处于“开发模式”：不会强制校验 Telegram 白名单，也不会强制校验 proxy allowlist/token。
- 在接管真实 PC 前，必须补齐：
  - `TELEGRAM_ALLOWED_USER_IDS`
  - proxy allowlist + 严格 token 校验
  - 保护 WS 传输（VPN / 隧道 / TLS 反向代理）
- 不要提交 `codex_config.json`（含密钥），已在 `.gitignore` 中忽略。
