# Codex 开发接入说明（给后续维护者）

本文档面向使用 OpenAI Codex（或其它编码代理）继续开发本仓库的同学，目标是让你在不了解历史背景的情况下也能快速上手、定位问题并安全迭代。

## 1. 这是什么项目

本仓库实现了一个“Telegram 控制入口 -> 多台机器执行 Codex”的最小控制面：

- Manager：`codex_manager.py`（Telegram + WebSocket 服务）
- Proxy：`codex_proxy.py`（WS 客户端 + 本机 `codex app-server` 常驻）

更完整的架构与协议见 [ARCHITECTURE.md](/root/telegram-bot/ARCHITECTURE.md)。

## 2. 关键约束

- 不要使用 opencode/opencode-server（弃用方案）。
- 不要在日志里输出 Telegram bot token（token 在 URL 路径中，尤其要避免 httpx 的 INFO 请求日志）。
- `codex_config.json`、`proxy_config.json` 均为本地机密配置文件：不提交到 Git（已在 `.gitignore`）。

## 3. 项目结构（你会经常碰到的文件）

- `codex_manager.py`：Manager 主程序（TG + WS 注册表 + 派单）
- `codex_proxy.py`：Proxy 主程序（WS 注册 + 本机执行）
- `codex_stdio_client.py`：Proxy 侧 stdio JSON-RPC 客户端（驱动 `codex app-server --listen stdio://`）
- `sessions.json`：Manager 保存每个 Telegram 会话的路由状态（运行后生成，gitignored）
- `sessions.json`：Manager 保存每个 Telegram 会话的路由元数据（chat -> proxy + per-proxy threadId），而 thread 内容由 Codex 自己持久化在 `~/.codex/`。
- `codex_config.json`：Manager 配置（运行时读取，gitignored）
- `proxy_config.json`：Proxy 配置（运行时读取，gitignored）
- `systemd/`：systemd unit 与 env 示例
- `scripts/`：启动脚本（使用本 repo 的 `.venv`）

兼容入口（尽量少改）：

- `bot.py` / `telegram_bot.py`：旧入口名，内部转到 `codex_manager.py`

## 4. 配置约定

### 4.1 Manager（Telegram + WS）

优先从 `codex_config.json` 读取（也支持环境变量覆盖），常用键：

- `telegram_bot_token`：Telegram bot token
- `manager_ws_listen`：WS 监听地址，例如 `0.0.0.0:8765`
- `default_proxy`：默认 proxy id
- `proxies`：proxy allowlist（当前代码 dev 模式不强制校验；后续可收紧）

systemd 推荐：

- `/etc/codex-manager.env`

### 4.2 Proxy（WS client + 本机 Codex）

Proxy 推荐使用 JSON（便于记忆和迁移）：

- `proxy_config.json`（本目录）或通过 `--config /path/to.json`
  - 支持 JSONC（可写 `//` 与 `/* */` 注释），便于在一个模板里用“注释开关”切换 `sandbox/approval_policy`

关键字段：

- `manager_ws`：例如 `ws://MANAGER_IP:8765`
- `proxy_id`：例如 `proxy1`
- `proxy_token`：可选（dev 模式可空）
- `codex_cwd`：本机执行目录
- `codex_bin`：默认 `codex`
- `http_proxy/https_proxy/no_proxy`：可选（某些环境访问 Telegram / 外网需要）

systemd 推荐：

- 统一使用 `WorkingDirectory` 下的 `proxy_config.json`（本目录示例：`/root/telegram-bot/proxy_config.json`）
- 不再使用 `/etc/codex-proxy.env` 与 `/etc/codex-proxy.json`

## 5. 常用命令（开发/排障）

### 5.1 安装依赖

```bash
./scripts/install.sh
```

### 5.2 语法检查

```bash
. .venv/bin/activate
python -m py_compile codex_manager.py codex_proxy.py codex_stdio_client.py
```

### 5.3 不启动 Telegram 的 WS 冒烟测试

终端 1：

```bash
. .venv/bin/activate
python codex_manager.py --ws-only --ws-listen 127.0.0.1:8765
```

终端 2：

```bash
. .venv/bin/activate
cp proxy_config.example.json proxy_config.json
editor proxy_config.json
python codex_proxy.py --config proxy_config.json
```

终端 3（派发一次并退出）：

```bash
. .venv/bin/activate
python codex_manager.py --ws-only --ws-listen 127.0.0.1:8765 --dispatch-proxy proxy1 --prompt ping --timeout 60
```

### 5.4 systemd 运行与查看日志

```bash
systemctl status codex-manager.service
journalctl -u codex-manager.service -f

systemctl status codex-proxy.service
journalctl -u codex-proxy.service -f
```

## 6. 当前实现范围（v0）

- WS 双向：Proxy 注册、Manager 派发、Proxy 回结果（最终文本）
- 会话粘性：`/use <proxy_id>`，以及默认选择策略
- `reset_next`：通过 `/use` 或 `/reset` 让下一次对话在 Proxy 侧创建新 thread

暂未实现（后续可迭代）：

- token 流式回传（`task_delta`）
- 负载/并发调度（capacity）
- 代理侧并发（当前 proxy 单任务 lock）
- 强制鉴权（当前为 dev 模式：allowlist/token 仅告警不拦截）
- WS TLS（建议走 VPN/隧道或反向代理）

## 7. 修改建议（迭代优先级）

1. 安全收紧：启用 `TELEGRAM_ALLOWED_USER_IDS`，并把 proxy token 校验改回强制
2. 可靠性：任务 ACK、超时重试、断线任务恢复策略
3. 体验：流式输出、消息节流编辑、长回复分段策略优化
4. 调度：capacity/health/标签路由（linux/windows、GPU/CPU 等）
