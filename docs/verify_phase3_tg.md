# 联调与全链路稳定性测试计划

更新时间：2026-03-01 04:42 CST

## 0. 目标与总原则

目标：先确保每个模块独立稳定，再进行模块组装验证，最后做系统全链路与稳定性测试。

原则：
- 先模块、后组装、再系统。
- 每阶段设置 Gate，未通过不得进入下一阶段。
- 每个关键链路都要有“正常路径 + 异常注入 + 恢复验证”。

## 1. 阶段划分与 Gate

### 阶段 A：模块级稳定性

A1 `manager`、A2 `node`、A3 `bot_comm` 分开验证。

Gate：
- 关键正常用例连续通过 20 次。
- 关键异常用例连续通过 10 次。
- 无进程崩溃、无死锁、无状态污染。

### 阶段 B：模块组装稳定性

B1 `manager <-> node`，B2 `manager <-> bot_comm`。

Gate：
- 组装用例通过率 100%。
- 故障恢复链路（重连/重试/重启）通过率 100%。

### 阶段 C：系统集成全链路

Telegram -> manager -> node -> codex -> manager -> Telegram。

Gate：
- 关键业务场景全部通过。
- 关键异常场景全部符合预期。

### 阶段 D：稳定性与回归

Soak + Burst + 故障注入。

Gate：
- 成功率与延迟满足阈值。
- 无资源泄漏趋势。

## 2. 测试矩阵

### A1 manager 模块

正常：
1. node 注册鉴权成功。
2. heartbeat 更新在线状态。
3. dispatch_once 返回结果。
4. appserver_call 返回结果。
5. approval 请求与决策闭环。
6. session/token 持久化读写正确。

异常注入：
1. token 缺失/错误/已废除。
2. 重复注册、乱序消息、孤儿 task_result。
3. task 超时、rpc 超时。
4. DB 写失败/锁冲突模拟。

验收：
- 错误可观测、可定位、可恢复。
- 状态不脏写。

### A2 node 模块

正常：
1. app-server 启动、连接 manager、注册成功。
2. 接收任务、上报 progress、上报 result。

异常注入：
1. cwd 不存在、codex bin 不存在。
2. manager 断开、网络抖动、重连退避。
3. queue 满、执行超时、审批超时。

验收：
- 失败有明确错误。
- 重试后可恢复。

### A3 bot_comm 模块

正常：
1. `/node /status /model /thread /token /approve` 路由正常。
2. callback 按钮行为与文本一致。

异常注入：
1. 未授权用户。
2. 参数缺失/JSON 非法。
3. manager core 缺失。
4. Telegram transient 失败重试。

验收：
- 提示准确，状态一致。

## 3. 模块组装测试

### B1 manager <-> node

验证：
1. 注册鉴权。
2. dispatch_once / dispatch_task。
3. appserver_call。
4. approval decision。

故障注入：
1. manager 重启。
2. node 重启。
3. 网络中断恢复。
4. 重复 task_id/重复回包。

验收：
- 最终态一致。
- 无重复执行。
- 恢复后链路可继续。

### B2 manager <-> bot_comm

验证：
1. 命令触发 manager 能力。
2. UI 回包和后端状态一致。

故障注入：
1. 无在线 node。
2. RPC timeout。
3. 鉴权失败。

验收：
- 用户侧反馈和后台事实一致。

## 4. 系统全链路场景

1. TG 文本消息，首次自动创建 thread。
2. 后续消息复用 thread。
3. approval 触发并 approve/decline。
4. token 生命周期：生成 -> 生效 -> 废除 -> 拒绝连接。
5. manager/node 重启后继续服务。

## 5. 稳定性与性能

- Soak：2-4 小时连续低压。
- Burst：短时并发冲击。
- Fault：周期性 kill/restart + 网络抖动。

观测指标：
- 成功率。
- P95 延迟。
- 错误率与错误分布。
- 恢复时间（RTO）。
- 资源趋势（进程内存、句柄）。

## 6. 执行顺序（固定）

1. A1 manager。
2. A2 node。
3. A3 bot_comm。
4. B1 manager<->node。
5. B2 manager<->bot_comm。
6. C 系统全链路。
7. D 稳定性回归。

## 7. 当前执行状态

- [x] 已完成：本地最小联调（manager ws-only + node 注册 + control servers 可见在线 node）。
- [x] 已完成：A1 manager 模块基础用例（含鉴权、超时观测、重连一致性）。
- [x] 已完成：A2 node 模块核心用例（启动异常、重连退避、queue 满载、超时注入）。
- [x] 已完成：A3 bot_comm 第一批模块测试（命令路由与异常提示）。
- [ ] 进行中：B1 manager<->node 组装稳定性（重启恢复场景，存在超时阻塞）。
- [x] 已完成：B2 第一批 manager<->bot 组装冒烟。
- [x] 已完成：C 系统集成全链路本地预检（control 路径）。
- [ ] 进行中：D 稳定性回归（受上游网络波动阻塞）。

### A1 第一批结果（已执行）

1. `A1-01` 缺失 token 注册 -> 拒绝：`PASS`
2. `A1-02` 错误 token 注册 -> 拒绝：`PASS`
3. `A1-03` 正确 token 注册 -> online 可见：`PASS`
4. `A1-04` token 废除后重连 -> 拒绝：`PASS`

结果摘要：
- 当前批次通过率：`4/4`
- 当前阻塞：无
- 下一批：`dispatch_once / appserver_call / timeout / orphan_result`

### A1 第二批结果（已执行）

1. `A1-05` appserver `thread/list` 正常调用：`PASS`
2. `A1-06` appserver `thread/read` 缺少 `threadId`：`PASS（预期错误）`
3. `A1-07` dispatch 调用链路闭环：`PASS（错误透传符合预期）`

判定说明（control 接口语义）：
- control `type=appserver/dispatch` 的外层 `ok=true` 表示“控制面调用成功返回”；
- 业务成败以内层 `result.ok` 判定；
- 因此 `A1-06` 为预期异常，判定为通过。

结果摘要：
- 第二批通过率：`3/3`
- 当前累计：`7/7`
- 下一批：`timeout / orphan_result / reconnect consistency`

### A1 第三批结果（已执行）

1. `A1-08` dispatch 超时语义检查：`OBSERVED`  
   - 现象：返回 `{\"ok\": false, \"type\": \"dispatch\", \"error\": \"TimeoutError: \"}`  
   - 结论：超时被正确触发，但与第二批“外层 ok=true 内层 result.ok=false”的返回形态不一致。
2. `A1-09` 同 node_id 重连一致性：`PASS`  
   - `servers.online` 中同一 node_id 仅出现一次。

问题记录：
- `dispatch/appserver` 控制面返回语义需统一：\n
  当前存在两种风格（外层失败 vs 外层成功内层失败），后续在 B1 前统一约定并回归。

### A2 第一批结果（已执行）

1. `A2-01` `cwd` 不存在：`PASS（预期失败）`
   - 现象：`FileNotFoundError: ... '/not/exist/path'`
2. `A2-02` `codex_bin` 不存在：`PASS（预期失败）`
   - 现象：`FileNotFoundError: ... '/not/exist/codex'`

改进建议：
- 当前表现为 Python traceback 直接退出；\n
  建议后续改为结构化错误日志 + 明确 exit code，便于运维识别。

### A2 第二批结果（已执行）

1. `A2-03` manager 不可达时重连退避：`PASS`
   - 观测到重连间隔递增并封顶（约 `0.5s -> 0.8s -> 1.4s -> 2.5s -> 4.2s -> 7.1s -> 10.0s`）。
2. `A2-04` manager 恢复后自动注册：`PASS`
   - manager 启动后无需人工干预，node 自动完成 register 并进入 online。

结果摘要：
- 第二批通过率：`2/2`
- 当前累计：`4/4`

### A2 第三批结果（已执行）

1. `A2-05` queue 满载（`max_pending=1`）复测：`PASS`
   - 方法：先预热 `thread_id`，再并发两条 `dispatch_text`。
   - 结果：至少一条请求返回 `result.error = "node queue full (max=1)"`。
   - 证据：
     - node 日志：`op=exec.enqueue ... pending=1 max_pending=1`
     - manager 日志：`op=ws.recv ... type=task_result ... ok=False`
2. `A2-06` 执行超时（长任务 + control timeout=15s）：`PASS（预期超时）`
   - 结果：并发另一条请求返回 `TimeoutError`，与预期一致。

问题记录：
- 当前 `dispatch_text` 并发压测中，两个请求完成形态可能不同（一个 queue full，一个 timeout），属于预期竞争结果。
- `dispatch` 用例不适合直接测 queue 满载，因为其前置 `thread/start` 会引入额外超时干扰。

### A3 第一批结果（已执行）

1. `A3-01` 未授权用户执行 `/token`：`PASS`
   - 返回 `unauthorized`。
2. `A3-02` `/token` 缺少参数：`PASS`
   - 返回 usage 文案。
3. `A3-03` `/approve` 缺少参数：`PASS`
   - 返回 usage 文案。
4. `A3-04` node callback 选择离线 node：`PASS`
   - callback alert 含 `offline`。
5. `A3-05` `/token generate` 成功路径：`PASS`
   - 调用了 `node_auth.generate`，回包含 `token created` 和 token_id。

结果摘要：
- 第一批通过率：`5/5`
- 当前累计：`5/5`
- 测试日志：`/tmp/a3_batch1.log`

### B1 第一批结果（已执行，含复测）

1. `B1-01` manager 重启后 node 自动重连：`PASS`
2. `B1-02` 同端口同 DB 恢复后 online 一致性：`PASS`
3. `B1-03` 重连后 appserver `thread/list`（cold timeout=60）：`PASS`
4. `B1-04` 重连后 appserver `thread/list`（warm timeout=20）：`FAIL`
   - 现象：control 返回 `{"ok": false, "type": "appserver", "error": "TimeoutError: "}`。
   - 复测结论：该问题可稳定复现。

阻塞项：
- node 重连后 appserver 响应性能/可用性未稳定，导致 B1 Gate 暂未通过。
- 相关日志：
  - `/tmp/b1_mgr2.log`
  - `/tmp/b1_node.log`
  - `/tmp/b1r_mgr2.log`
  - `/tmp/b1r_node.log`
  - `/tmp/b1fix_mgr2.log`
  - `/tmp/b1fix_node.log`

补充结论（修复后复测）：
- 已修复 node 端 `appserver_request` 阻塞 ws 收包循环的问题（改为后台任务 + 串行锁）。
- 复测仍出现 `warm timeout=20` 超时，日志显示 `thread/list` 本身常接近 `20s`，属于当前环境性能门限问题而非 ws 心跳掉线问题。

### B2 第一批结果（已执行）

1. `B2-01` `/status` 未授权：`PASS`
2. `B2-02` `/status` 未选 node：`PASS`
3. `B2-03` `/status` 选中 node 但离线：`PASS`
4. `B2-04` `/status` manager core 缺失：`PASS`
5. `B2-05` `/config_value_write` 非法 JSON：`PASS`
6. `B2-06` `/config_value_write` 后端 TimeoutError 透传：`PASS`

结果摘要：
- 第一批通过率：`6/6`
- 测试日志：`/tmp/b2_batch1.log`

### 命名统一回归（已执行）

1. `A2-05R` queue 满载错误文案统一为 `node`：`PASS`
   - 响应中可见：`node queue full (max=1)`。
   - 日志文件：
     - `/tmp/a205f_r1.json`
     - `/tmp/a205f_r2.json`
     - `/tmp/a205f_mgr.log`
     - `/tmp/a205f_node.log`

### C 阶段预检（已执行）

1. `C-01` 全仓 Python 语法编译检查：`PASS`
   - `py_compile` 通过，覆盖 `54` 个 `.py` 文件。
2. `C-02` 本地全链路预检（control -> manager -> node -> codex -> manager）：`PASS`
   - `dispatch_text timeout=90` 返回 control `ok=true`。
   - 耗时约 `49s`。
   - 结果文件：`/tmp/c_precheck_resp.json`
   - 日志：
     - `/tmp/c_precheck.log`
     - `/tmp/c_precheck_mgr.log`
     - `/tmp/c_precheck_node.log`

### D 阶段短压测（已执行）

1. `D-01` 5 次顺序 `dispatch_text`（timeout=90）：`OBSERVED`
   - control 层成功率：`5/5`
   - 业务层成功率：`0/5`（均为 `result.ok=false`）
   - P95 latency（control 成功样本）：约 `56.36s`
2. 失败主因：
   - node/codex 日志一致报错：`stream disconnected before completion`，
   - 上游地址：`https://chatgpt.com/backend-api/codex/responses`
   - 伴随 app-server 内部重试 `Reconnecting... 1/5 ... 5/5`。

结论：
- 当前 D 阶段阻塞为“外部上游网络/连接稳定性”，非 manager<->node 协议闭环故障。
- 数据与日志：
  - `/tmp/d_short_soak_results.json`
  - `/tmp/d_short_soak.log`
  - `/tmp/d_short_mgr.log`
  - `/tmp/d_short_node.log`

### 错误分类增强（已执行）

1. 在 node `task_result` 失败路径增加结构化字段：
   - `error_kind`
   - `retriable`
2. 覆盖路径：
   - app-server 异常
   - 超时异常
   - queue 满载
   - 参数缺失/内部入队失败
3. manager 侧错误展示增强：
   - 失败文本增加 `kind=<...>, retriable=<yes/no>` 片段，便于运维快速定位。

回归验证：
1. 单次 E2E 请求：`result.ok=true`（未触发失败分类），见：
   - `/tmp/e2e_errfields_resp.json`
2. 强制失败并发压测：`PASS`
   - `/tmp/errkind_r1.json` 与 `/tmp/errkind_r2.json` 均包含：
     - `result.error_kind=queue_full`
     - `result.retriable=false`
3. 上游断流复测分类：`PASS`
   - `/tmp/d_recheck_1.json`、`/tmp/d_recheck_2.json` 均为：
     - `result.ok=false`
     - `result.error_kind=upstream_disconnect`
     - `result.retriable=true`
   - 日志：
     - `/tmp/d_recheck_mgr.log`
     - `/tmp/d_recheck_node.log`

### control 语义统一回归（已执行）

目标：
- 外层 `ok` 仅表示控制面收发成功；
- 业务成败统一看 `result.ok`。

结果：
1. `dispatch_text` 超时场景：`PASS`
   - 外层 `ok=true`，内层 `result.ok=false`。
   - `/tmp/ctl_sem_1.json`
2. `appserver thread/read` 参数异常：`PASS`
   - 外层 `ok=true`，内层为失败结果。
   - `/tmp/ctl_sem_2.json`
3. `dispatch` 缺少 `node_id`（请求非法）：`PASS`
   - 外层 `ok=false`（仍保留参数校验失败语义）。
   - `/tmp/ctl_sem_3.json`

日志：
- `/tmp/ctl_sem_mgr.log`
- `/tmp/ctl_sem_node.log`

### 命名统一（增量清理）

1. 运行日志键名统一：
   - 统一使用 `node_id=`
   - 回归日志检查：
     - `/tmp/logkey_mgr.log`
     - `/tmp/logkey_node.log`
   - 结果：日志键名稳定为 `node_id=`
2. node 进程标识统一：
   - logger 名称：`codex_node`
   - app-server client_name：`codex_node:<node_id>`
3. 编译回归：
   - 全仓 `py_compile` 通过（`54` 文件）。

4. 脚本与文档参数统一（兼容不破坏）：
   - `scripts/verify_phase2_ws.sh`：入参标识切换为 `node_id`，请求字段改用 `node_id`。
   - `scripts/verify_phase2_appserver_rpc.sh`：同上。
   - `AGENTS.md` 示例：`/use <node_id>`。

脚本冒烟结论：
1. 直接冒烟失败案例：`FAIL node not online`
   - 根因：本地配置中的 `codex_cwd` 指向不存在路径（`/root/telegram-bot`），node 启动即退出。
2. 对照复测：`PASS`
   - 使用显式正确配置（`codex_cwd=/root/work/telegram-bot`）后，
   - `verify_phase2_appserver_rpc.sh n1` 通过。
   - 记录：`/tmp/script_nodeid_smoke_ok.log`

补充修复（本轮）：
1. 配置模板修正：
   - `node_config.example.json` 的 `codex_cwd` 改为 `/root/work/telegram-bot`。
   - `node_config.void.json` 同步改为 `/root/work/telegram-bot`（用于本地联调一致性）。
2. phase2 脚本增强：
   - `verify_phase2_ws.sh` / `verify_phase2_appserver_rpc.sh` 增加 `WAIT_ONLINE` 等待上线逻辑，避免 node 刚启动时误判离线。
3. 启动回归修复：
   - 修复 `node/entry/app.py` 构造参数误写（旧命名），恢复 node 正常启动。
4. 命名统一增量：
   - 文档中的关键可见术语继续统一到 `node`（`DEVELOPMENT.md`、`ARCHITECTURE.md`、`AGENTS.md`）。

5. Node 新入口与兼容：
   - 新增 `codex_node.py`（推荐入口），保留 `codex_node.py` 兼容入口。
   - 新增 `scripts/run_node.sh`（推荐启动脚本），保留 `scripts/run_node.sh` 兼容脚本。
   - 兼容脚本 `scripts/run_node.sh` 已内部切换到 `codex_node.py`，与新入口行为一致。
   - `node/entry/app.py` 主类统一为 `CodexNodeAgent`，已移除旧版命名兼容层。
   - 快速验证：`codex_node.py --help` 与 `codex_node.py --help` 均正常返回。
   - 在线注册验证：`codex_node.py --config ...` 可在 40s 内上线（`PASS`）。
     - 记录：`/tmp/entry2_summary.log`
     - 日志：`/tmp/entry2_mgr.log`、`/tmp/entry2_node.log`

## 8. 记录模板（每轮必填）

每个用例记录：
1. case_id
2. preconditions
3. steps
4. expected
5. actual
6. result (PASS/FAIL)
7. logs(trace_id/node_id/task_id)
8. root_cause（若 FAIL）
9. fix_commit（若有）
10. retest_result
