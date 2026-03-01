# wsl-node-runtime

## 目的

在 WSL 或其它无 systemd 环境下，稳定运行一个或多个 node 实例。
该流程不依赖 `agent-node.service` / `agent-node@.service`。

## 适用场景

- `systemctl` 不可用或不可控
- WSL 长驻 node 进程
- 同机多实例 node 运维

## 标准流程

1. 进入项目目录并确认依赖

```bash
cd /root/work/telegram-bot
test -x .venv/bin/python
test -f node_config.json
```

2. 用统一脚本启动实例（示例：两个配置）

```bash
nohup bash -lc 'cd /root/work/telegram-bot && scripts/run_node_forever.sh --config node_config.json --python .venv/bin/python' \
  >/tmp/node.master.daemon.out 2>&1 &

nohup bash -lc 'cd /root/work/telegram-bot && scripts/run_node_forever.sh --config node_config.void.json --python .venv/bin/python' \
  >/tmp/node.void.daemon.out 2>&1 &
```

3. 验证实例状态

```bash
ps -ef | rg 'codex_node.py --config'
ls -l run/node.*.json
```

4. 查看运行日志

```bash
tail -f /tmp/codex_node_forever.log
```

5. 停止实例（按配置精确停止）

```bash
pkill -f 'codex_node.py --config node_config.json'
pkill -f 'codex_node.py --config node_config.void.json'
```

## 约束

- 统一使用 `scripts/run_node_forever.sh`，不要新增“只绑定某个 node_id”的重复脚本。
- 本 skill 仅定义运行机制，不规定 node_id 命名；`master/void_1` 只是示例。
- 配置以 `node_config*.json` 为准，token、manager_ws、cwd 由配置文件决定。
