# AGENTS (for Codex)

这个文件只放“给编码代理的执行规则与代码定位”，不放详细运维步骤。
详细的安装、运行、测试、部署命令统一维护在 [README.md](/root/work/telegram-bot/README.md)。

## 1. 执行优先级

1. 先读 [ARCHITECTURE.md](/root/work/telegram-bot/ARCHITECTURE.md) 理解链路。
2. 按本文件约束改代码并做最小验证。
3. 详细命令与环境参数以 README 为准，不在本文件重复。

## 2. 当前硬约束（必须遵守）

1. 概念统一为 `node`，不再新增 `proxy` 命名。
2. 鉴权统一 `node_token`，不依赖 allowlist。
3. `/token <node_id>` 或 `/token generate node_id=<id>` 必须返回可直接落地的完整 `node_config.json`。
4. 生成的 `node_config.json` 不包含 `http_proxy/https_proxy/no_proxy` 字段。
5. `manager_ws` 优先公网地址（`CODEX_MANAGER_PUBLIC_WS`）。本地地址只可做说明，不写入可执行 JSON。
6. `/status` 必须同时体现：
- `thread_mode(current)`（当前 thread 实际模式）
- `execution_defaults(node)`（node 注册上报默认值）
- `session_thread` 与 `status_thread`（标注来源）
7. node 启动时 `codex_cwd` 不存在需自动创建并 warning；创建失败才退出。

## 3. 项目结构（代码定位）

```text
telegram-bot/
├── manager/
│   ├── entry/      # manager 入口、handler 注册、装配
│   ├── service/    # 调度/会话/进度核心逻辑
│   ├── infra/      # WS server、SQLite repo、迁移
│   └── auth/       # node token / TG 用户授权
├── node/
│   └── entry/      # node 入口、注册、app-server 桥接
├── bot_comm/
│   ├── handlers/   # /status /token /thread /model 等命令处理
│   ├── infra/      # Telegram outbox
│   └── stdio_client.py
├── scripts/        # 安装/验证/启动脚本（详细见 README）
├── systemd/        # systemd 单元（详细见 README）
└── docs/
```

## 4. 按需求找文件

1. 改 Telegram 命令：`bot_comm/handlers/*.py` + `manager/entry/app.py`
2. 改 node 注册/WS 协议：`node/entry/app.py` + `manager/infra/ws_node_server.py` + `manager/infra/node_registry.py`
3. 改调度/超时/进度：`manager/service/manager_core.py` + `dispatch_service.py` + `progress_render_service.py`
4. 改会话与 thread 绑定：`manager/service/session_service.py` + `manager/infra/repo_sessions.py`
5. 改 token 鉴权：`manager/auth/node_auth_service.py` + `manager/infra/repo_tokens.py` + `bot_comm/handlers/token_handlers.py`
6. 改 `/status`：`bot_comm/handlers/status_handlers.py`

## 5. 文档分工（防混乱）

1. 放在 AGENTS.md：
- 编码代理约束
- 代码结构与定位
- 关键行为契约（例如 `/status`、`/token`）

2. 放在 README.md：
- 安装/启动/测试/调试/部署命令
- 环境变量、systemd、WSL 运行说明
- 运维排障步骤

如果两处冲突，以 README 的运行步骤为准，并在同一提交里修正 AGENTS 的引用。
