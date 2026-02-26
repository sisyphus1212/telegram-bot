# 架构说明

## 总览

本仓库实现了一套“最小但可用”的控制面：

- **Manager**（`codex_manager.py`）：一个进程同时运行
  - Telegram bot（polling）
  - WebSocket 服务（Proxy 在这里注册；Manager 在这里派发任务、接收结果）
- **Proxy**（`codex_proxy.py`）：每台机器一个进程
  - 通过 WebSocket 主动连接 Manager
  - 启动并保持本机 `codex app-server` 常驻（stdio JSON-RPC）
  - 执行任务并回传结果

最终实现：**一个 Manager** 控制 **多台 Proxy**（跨多机器）。

## 数据流

### 启动流程

1. Manager 先启动：
   - 绑定 WS：`CODEX_MANAGER_WS_LISTEN`（例如 `0.0.0.0:8765`）
   - 如果有 `TELEGRAM_BOT_TOKEN`，启动 Telegram polling
2. 每台机器的 Proxy 后启动：
   - 连接 `CODEX_MANAGER_WS`（例如 `ws://MANAGER_IP:8765`）
   - 发送 `register`（proxy_id, token）
   - 收到 `register_ok` 后开始等待任务

### 一次对话（一个 turn）

1. 用户在 Telegram 发消息
2. Manager 收到消息后选择一个 Proxy：
   - 如果该聊天已通过 `/use <proxy_id>` 绑定到某个 Proxy 且在线：用它
   - 否则如果 `CODEX_DEFAULT_PROXY` 在线：用它
   - 否则：选择第一个在线 Proxy
3. Manager 向该 Proxy 下发 `task_assign`
4. Proxy 在本机保证该聊天对应的 Codex thread 存在，然后执行一次 turn：
   - 需要时：`thread/start`
   - `turn/start`
   - 等待 `turn/completed`
5. Proxy 回传 `task_result` 给 Manager
6. Manager 把最终文本回写到 Telegram（编辑或回复消息）

## 状态与持久化

- Manager 持久化 **路由状态** 到 `sessions.json`：
  - `proxy`：当前聊天选用的 proxy（空字符串表示自动选择）
  - `reset_next`：下一次对话是否要求重置远端 thread（由 `/use` 和 `/reset` 触发）
- Proxy 在内存中维护 **thread 映射**：
  - `thread_key`（来自 Telegram chat+user） -> `thread_id`
  - 当收到 `reset_thread=true` 时，proxy 丢弃映射，下次会创建新的 thread

该设计避免把 Codex thread id 泄漏到 Manager，也让“在机器上执行”的逻辑尽可能本地化。

## WS 协议（最小 v0）

所有消息都是 JSON，必须包含 `type` 字段。

### Proxy -> Manager（上行）

- `register`：
  - `{"type":"register","proxy_id":"proxy1","token":"..."}`
- `heartbeat`：
  - `{"type":"heartbeat","proxy_id":"proxy1"}`
- `task_result`：
  - 成功：`{"type":"task_result","task_id":"...","ok":true,"text":"..."}`
  - 失败：`{"type":"task_result","task_id":"...","ok":false,"error":"..."}`

### Manager -> Proxy（下行）

- `register_ok` / `register_error`
- `task_assign`：
  - `{"type":"task_assign","task_id":"...","thread_key":"tg:<chat>:<user>","prompt":"...","reset_thread":false}`

## 安全说明（当前开发模式）

当前为了快速打通链路，默认处于“开发模式”：

- 不设置 `TELEGRAM_ALLOWED_USER_IDS` => 任意 Telegram 用户都能控制机器人
- Proxy allowlist/token 不强制校验（仅告警）

在接管真实 PC 前，建议至少做到：

- Telegram 白名单（`TELEGRAM_ALLOWED_USER_IDS`）
- Proxy allowlist + token 强校验（把 Manager 的 dev mode 改回强制）
- WS 链路保护（VPN / SSH 隧道 / TLS 反向代理）
