# 阶段 3 验证：Telegram <-> Manager <-> Proxy <-> Codex <-> Telegram

本阶段只在 **阶段 1**（proxy 本机 codex ping/pong）和 **阶段 2**（manager<->proxy WS dispatch）都通过后再做。

## 1. Manager 必须成功进入 polling

在 manager 机器上：

```bash
journalctl -u codex-manager.service -n 200 --no-pager | rg 'Telegram polling started|Telegram start failed'
```

验收：
- 必须看到 `Telegram polling started`
- 不应持续刷 `Telegram start failed`

## 2. TG 基础验证（不依赖 proxy）

在 Telegram 对话里：
1. `/ping`

验收：
- 必须立即回复 `pong`

## 3. TG 端到端验证（依赖 proxy）

在 Telegram 对话里：
1. `/proxy_list` 确认目标 proxy 在线（例如 `proxy27`，旧命令 `/servers` 仍可用）
2. `/proxy_use proxy27`（旧命令 `/use proxy27` 仍可用）
3. `/thread_start`（可选：显式新建 thread；不做也行，首次发文本会自动创建）
4. 发送文本：`ping`（连续发 5 条也可以，用于验证“可连续下发”）

验收：
- 先出现占位消息：`working (proxy=..., threadId=...) ...`
- 随后占位消息被编辑为：`[proxy_id] ...`（成功或错误）

## 4. 关键日志（必须能串起来）

在 manager 机器上抓关键日志：

```bash
journalctl -u codex-manager.service -n 400 --no-pager | rg 'op=tg.update|op=tg.send|op=tg.edit|op=dispatch.enqueue|op=ws.recv|op=ws.send'
```

你应该能看到同一个 `trace_id` 一路贯穿：
- `op=tg.update trace_id=...`
- `op=tg.send ... kind=placeholder ... trace_id=...`
- `op=dispatch.enqueue trace_id=...`
- `op=ws.send ... trace_id=...`
- `op=ws.recv ... trace_id=...`
- `op=tg.edit ... trace_id=... kind=result`

会话(thread)命令链路（可选观察）：
- `cmd /thread_start ...`
- `op=appserver.send ... method=thread/start ...`
- `op=ws.send ... type=appserver_request ...`
- `op=ws.recv ... type=appserver_response ...`
