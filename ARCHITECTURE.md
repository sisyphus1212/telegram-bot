# 架构说明

## 总览

本仓库实现了一套“最小但可用”的控制面：

- **Manager**（`codex_manager.py`）：一个进程同时运行
  - Telegram bot（polling）
  - WebSocket 服务（Node 在这里注册；Manager 在这里派发任务、接收结果）
- **Node**（`codex_node.py`）：每台机器一个进程
  - 通过 WebSocket 主动连接 Manager
  - 启动并保持本机 `codex app-server` 常驻（stdio JSON-RPC）
  - 执行任务并回传结果

最终实现：**一个 Manager** 控制 **多台 Node**（跨多机器）。

## 数据流

### 启动流程

1. Manager 先启动：
   - 绑定 WS：`CODEX_MANAGER_WS_LISTEN`（例如 `0.0.0.0:8765`）
   - 如果有 `TELEGRAM_BOT_TOKEN`，启动 Telegram polling
2. 每台机器的 Node 后启动：
   - 连接 `CODEX_MANAGER_WS`（例如 `ws://MANAGER_IP:8765`）
   - 发送 `register`（node_id, token）
   - 收到 `register_ok` 后开始等待任务

### 一次对话（一个 turn）

1. 用户在 Telegram 发消息
2. **当前设计：必须先 `/node` 选择机器**
3. Manager **立即**向该 Node 下发 `task_assign`（不等待执行结果）
4. Node 收到后：
   - 若 “Codex 回答前未完成任务” 已达到上限（默认 10）：立刻回 `task_result(ok=false, error="node queue full")`
   - 否则立刻回 `task_ack`（表示已入 node 队列），并把任务入队
5. Node 在本机按收到顺序（FIFO）把任务喂给 `codex app-server`，并回 `task_result`：
   - 运行中会把关键 app-server 通知转成 `task_progress`（例如 `turn/plan/updated`、`item/started`、重试中的 `error`）
   - 需要时：`thread/start`
   - `turn/start`
   - 等待 `turn/completed`
6. Manager 收到 `task_progress` 后会按 `task_id` 找到 placeholder，并在 Telegram 上节流更新“增量进度日志”（默认约 5 秒最多编辑一次）
7. Manager 收到 `task_result` 后按会话配置回写 Telegram：
   - `replace`：结果覆盖 placeholder
   - `send`：placeholder 改为 done/failed，结果单独发送

## 状态与持久化

- Manager 持久化 **路由状态** 到 `manager_data.db`：
  - `node`：当前聊天选用的 node（空字符串表示未选择）
  - `by_node.<node_id>.current_thread_id`：当前聊天在该 node 上的 current threadId（历史字段名保留）
- Codex thread 内容由 Codex 自己保存在 node 机器的 `~/.codex/`，本项目只保存 threadId “指针/路由”。

该设计避免把 Codex thread id 泄漏到 Manager，也让“在机器上执行”的逻辑尽可能本地化。

## WS 协议（最小 v0）

所有消息都是 JSON，必须包含 `type` 字段。

### Node -> Manager（上行）

- `register`：
  - `{"type":"register","node_id":"node1","token":"..."}`
- `heartbeat`：
  - `{"type":"heartbeat","node_id":"node1"}`
- `task_result`：
  - 成功：`{"type":"task_result","task_id":"...","ok":true,"text":"..."}`
  - 失败：`{"type":"task_result","task_id":"...","ok":false,"error":"..."}`
- `task_progress`：
  - `{"type":"task_progress","task_id":"...","event":"item/started","stage":"command","summary":"执行命令: ip -4 addr show"}`
- `appserver_response`（app-server JSON-RPC 透传结果）：
  - `{"type":"appserver_response","req_id":"...","ok":true,"result":{...}}`
  - `{"type":"appserver_response","req_id":"...","ok":false,"error":"..."}`

注意：当 node 队列已满时，node 会直接返回 `task_result(ok=false, error="node queue full (max=10)")`，Manager 会将错误回写到 Telegram。

### Manager -> Node（下行）

- `register_ok` / `register_error`
- `task_assign`：
  - `{"type":"task_assign","task_id":"...","thread_key":"tg:<chat>:<user>","thread_id":"<threadId>","prompt":"...","model":"gpt-5.3-codex","effort":"medium"}`
  - 说明：
    - `model/effort` 为可选字段，会在 node 侧转成 app-server 的 `turn/start` per-turn overrides。
    - 按 app-server 语义，turn/start 指定的 overrides 会写回到 thread 默认值，用于后续 turns。
- `appserver_request`（app-server JSON-RPC 透传）：
  - `{"type":"appserver_request","req_id":"...","method":"thread/list","params":{"limit":1}}`

## 安全说明

当前安全基线：

- Node 注册必须通过 token 鉴权（token 由 manager 维护在 `manager_data.db`）
- Telegram 用户授权由 `tg_allowed_users` 表控制（首次可用 `TELEGRAM_ALLOWED_USER_IDS` 引导种子数据）

在接管真实 PC 前，建议至少做到：

- 收紧 Telegram 授权用户（`tg_allowed_users`）
- 轮换并按节点绑定 token（`node_tokens`）
- WS 链路保护（VPN / SSH 隧道 / TLS 反向代理）
