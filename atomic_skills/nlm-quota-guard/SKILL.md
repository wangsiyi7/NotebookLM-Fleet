---
name: nlm-quota-guard
description: Inspect multi-account NotebookLM 5-hour and weekly quota health, per-action cost estimates (29 actions), reset countdowns, and auth readiness before dispatching heavy workloads. Strictly read-only with zero side effects unless live polling is explicitly requested.
---

# NLM Quota Guard（多账号额度守卫原子技能）

用于**任务派发前的只读摸底、成本预估与调度策略决策**，防止盲目触发音频/视频生成导致主账号（如 Ultra）5 小时或周额度被意外击穿。

## 前置依赖
- `notebooklm-fleet` 服务运行于 `http://127.0.0.1:8765`，或已挂载本机 Stdio MCP (`notebooklm-fleet`)。

## 执行步骤

1. **优先读取库内最近快照（零网络开销）**
   - MCP 路径：调用 `fleet_quota_overview()`，获取所有启用账号的 `tier`、`nlm_auth_status`、`official_oauth_status`、`five_hour_remaining_pct`、`weekly_remaining_pct`、`five_hour_resets_at`、`weekly_resets_at` 及 `actions`（29 项动作估算消耗）。
   - REST 路径：`GET http://127.0.0.1:8765/api/accounts` 和 `GET http://127.0.0.1:8765/api/dashboard`。
2. **快照缺失或超过 15 分钟时按需刷新实测**
   - 仅当目标账号的 `latest_polled_at` 为空、明显过期，或用户明确要求实时测额时：
     - 单个账号：`fleet_poll_quotas(alias="<alias>")` 或 `POST /api/accounts/{id}/poll-quota`
     - 全池巡检：`fleet_poll_quotas()` 或 `POST /api/accounts/poll-all`
   - *保护原则*：禁止高频轮询，避免不必要地触发上游会话请求。
3. **核算目标操作是否安全可执行**
   - 读取对应动作的 `estimated_cost_percent` 与 `remaining_deferred_artifact_generations`：
     - `cinematic_video` (~14–20%) / `video` (~2–15%) / `audio` (~2–12%) / `slide_deck` (~10%) 属于高耗能重任务，优先确认 Ultra 账号充足。
     - `ingest_*` 资料入库动作不消耗 Studio 算力窗口，可优先分流到 Official REST/MCP 就绪账号。
   - 校验扣减后是否仍高于 `safety_buffer_pct`（默认 `10%`）且 `weekly_remaining_pct >= 5%`。
4. **账号异常转交**
   - 若账号 `nlm_auth_status != "ok"` 或 `official_oauth_status != "ok"`，停止估算并引导使用 `nlm-account-ops` 恢复会话，严禁索要用户明文密码或打印任何本地 Cookie/Token。

## 输出规范
向调用方（人类或其他 AI 编程 Agent）返回清晰的结构化判断：
- 各账号 `tier`、双通道就绪态、`5h 剩余%`（附重置倒计时）、`周剩余%`；
- 目标任务预计消耗与推荐路由策略（`conserve_ultra` / `ultra_first` / `balanced`）；
- 是否建议立即执行，或预估需要等待延时队列（`deferred_later`）/ 窗口重置。
