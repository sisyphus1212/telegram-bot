# Telegram Bot - Codex 集成

连接 Telegram Bot 到 Codex（通过 `codex app-server`），实现 AI 对话与本机操作功能。

## 文件说明

- `bot.py` - 主程序文件
- `codex_app_server_client.py` - `codex app-server` stdio JSON-RPC 客户端
- `codex_app_server_probe.py` - 自检脚本（先单独把 app-server 链路跑通）
- `codex_config.example.json` - 配置示例（不要提交真实 token）
- `requirements.txt` - Python 依赖
- `scripts/install.sh` - 创建 venv 并安装依赖
- `scripts/run.sh` - 使用 venv 启动 bot
- `systemd/telegram-bot.service` - systemd 服务文件
- `systemd/telegram-bot.env.example` - systemd 环境变量示例
- `log/bot.log` - 运行日志（运行后生成）
- `sessions.json` - 会话存储文件（运行后生成）

## 依赖

需要本机可执行的 `codex`（`codex --version`）。

## 使用方法

### 1. 安装

```bash
./scripts/install.sh
```

### 2. 配置

本项目默认会在 bot 启动时拉起一个常驻的 `codex app-server` 子进程（stdio JSON-RPC），并在 `sessions.json` 中保存每个聊天的 `threadId`。

推荐用环境变量（systemd 也会用）：

- `TELEGRAM_BOT_TOKEN`: Telegram Bot token
- `TELEGRAM_ALLOWED_USER_IDS`: 允许使用的用户ID列表(逗号分隔)，为空表示不限制
- `TELEGRAM_PC_MODE_DEFAULT`: `1/0`，新建会话默认是否开启 PC 模式
- `CODEX_BIN`: codex 可执行文件名/路径（可选）
- `CODEX_CWD`: codex 工作目录（可选）

### 3. 自检

先单独验证 app-server 协议是否正常：

```bash
./scripts/run.sh  # 需要先配置 TELEGRAM_BOT_TOKEN，否则会报错
```

或者只测 Codex app-server：

```bash
. .venv/bin/activate
python codex_app_server_probe.py --prompt ping
```

### 4. 前台启动

```bash
./scripts/run.sh
```

### 5. 作为 Linux 服务（systemd）

```bash
sudo cp systemd/telegram-bot.service /etc/systemd/system/telegram-bot.service
sudo cp systemd/telegram-bot.env.example /etc/telegram-bot.env
sudo editor /etc/telegram-bot.env

sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot.service

sudo systemctl status telegram-bot.service
sudo journalctl -u telegram-bot.service -f
```

## 特性

✅ 日志记录到文件
✅ 会话持久化（重启后保留）
✅ 多用户支持
✅ 错误处理
✅ 状态监控

## 故障排除

1. Bot无响应：查看 `log/bot.log` 或 `journalctl -u telegram-bot.service -f`
2. 连接失败：确认本机 `codex` 可用（`codex --version`）
3. 风险控制：建议设置 `TELEGRAM_ALLOWED_USER_IDS` 只允许你自己使用
