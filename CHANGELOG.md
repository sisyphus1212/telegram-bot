# Changelog

## 2026-03-01

### Architecture / Refactor
- Split codebase into clearer `manager` / `node` / `bot_comm` modules and stabilized node reconnect notification path.

### Manager / Ops
- Added `/manager` runtime command and expanded output with public WS address, runtime context, and proxy-related info.
- Added `scripts/hot_upgrade_manager.sh` and improved fetch resiliency for transient network timeout.
- Added inflight-task guard before hot upgrade restart (abort by default when inflight tasks exist; `FORCE=1` to override).

### Task Runtime / Delivery
- Changed long-task timeout behavior to idle-timeout semantics.
- Passed per-task timeout from Telegram path to node dispatch to avoid implicit 120s cap.
- Improved Telegram outbox reliability:
  - prioritize result/error delivery over low-priority progress
  - split oversized messages automatically
  - keep approval action card intact by splitting detail and action messages
- Progress updates switched to batched push model (per 5 changes).

### Thread / UX
- `/thread current` now shows current thread mode info (`model/sandbox/approval/cwd`) with node/session defaults.
- `/thread list` index tokens can be used directly by `resume/read/archive/unarchive`; supports on-demand index rebuild.
- Added `/thread fork` wizard and `/thread` shortcut button:
  - path-first flow
  - sandbox/approval selection
  - final confirm with full command preview
- Added `/thread start` wizard with same interaction style as fork.

### Node / App-Server Protocol
- Fixed app-server request handling gap:
  - support `RequestId` as `string|integer`
  - handle `item/tool/call` and `item/tool/requestUserInput`
  - keep legacy approval request compatibility mapping
  - add detailed unsupported-request logging (`method`, `id_type`, `params_keys`)
- This addresses root cause behind `Custom tool call output is missing for call id ...` caused by incomplete server-request handling.

### Branching Policy
- Created long-lived stable branch: `origin/stable`.
- Future feature work should branch from `stable` and merge via PR into `stable`.

