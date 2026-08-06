# ADR-0008：持久化任务执行核心（S1a）——`run_tool_loop` 作 Step 内核 + typed event 统一

- **状态**：✅ 已采纳（S1a 代码已落地，全量 589 单测零回归）
- **日期**：2026-08-06
- **背景**：把单次对话工具回路升级为可恢复的多步任务执行核心
- **关联**：ADR-0006（任务系统增量路线）、ADR-0002（LangGraph 工具回路）、
  `docs/specs/agent-framework-upgrade.md` §3/§8、`tasks/plan.md`

## 背景

ADR-0006 决定任务系统基于 SQLModel 增量建设，本 ADR 记录其第一切片 S1a 的**落地架构决策**：

- 现状：`run_agent` → `run_tool_loop` 是「单次对话内同步工具循环」，任务随对话消亡，不可恢复、不可跨多步编排。
- `run_agent` 与 `run_agent_stream` 两套高度相似流程（product-strategy §3 缝隙 #6），策略增多后易行为漂移。
- S1a 目标：持久化 `Task/Step/Run` 三层实体 + 状态机，让 Agent 能处理「给定目标 → 拆步 → 逐步执行（含 HITL 暂停）→ 可恢复完成」的业务任务，且不重写已验证的工具回路。

## 决策

1. **`run_tool_loop` 作 Step 执行内核，不重写工具回路**：Step 执行器 = 拼消息 + 重算能力集 +
   调 `graph.run_tool_loop` + 采审计。治理链与 HITL 已绑定在现有回路中，重写引入回归风险。
2. **typed event 统一两套 run 路径**：`run_agent` / `run_agent_stream` 产出同一事件流 →
   审计 / 流式输出 /（S1b）前端共用，闭合缝隙 #6。事件类型：`task_created / plan_produced /
   step_started / tool_called / approval_requested / step_completed / step_failed /
   task_done / task_paused`，带 `trace_id` 接 OTel。
3. **每步执行前重算 `CapabilitySet`**：复用 `execute_approved_tool` 已验证模式（重算能力集 +
   白名单验证 + 脱敏）抽成共享函数——任务创建后用户权限变更不再继续越权执行（ADR-0006 红线）。
4. **对话触发**：`run_agent` 识别「启动任务：{目标}」→ 创建 Task；未识别时退化为现有 `run_agent`（零回归）。
5. **状态机驱动续跑**：Task/Step 状态显式持久化，`paused`（等审批/等输入）可跨进程恢复；
   提供未完成任务扫库续跑（`resume_pending_tasks`），应用启动自动接入留待后续切片。

## 落地

- **实体**：`Task`（trigger/status/capability_snapshot_json）→ `Step`（kind/idempotency_key/retry_count）→ `Run`（input/output/duration/tool_calls），`governance/models.py`。
- **事件**：`agent/events.py` 轻量进程内总线（发布/订阅 + trace_id）。
- **状态机**：`draft → planning → running ⇄ paused → completed / failed / cancelled`。
- **验证**：`make test-unit` 全量 589 passed 零回归；对话触发 → 执行 → 审批恢复端到端通过。

## 备选方案

| 方案 | 否决理由 |
|------|---------|
| 引入 Temporal/Dagster 独立工作流引擎 | 已在 ADR-0006 否决（重依赖、与现有 session/审批体系割裂） |
| 重写工具回路为任务专用内核 | 治理链/HITL/审计已绑定 `run_tool_loop`，重写高风险且双维护 |
| 两条 run 路径各自维护事件流 | 缝隙 #6 行为漂移依旧，审计/流式双份实现 |

## 后果

- 任务执行与普通对话共享**同一条治理链**（单一真相源），权限语义天然一致。
- S1b（TaskSchedule 定时/事件触发 + 前端 SSE 进度页）、S2 记忆层、S3 skill 演化均构建在本 ADR
  的事件与状态机之上。
- 遗留边界（后续切片）：对话维度 event 统一尚未闭合（任务维度已闭合）；LLM 自动拆多步 plan
  （当前为显式单步触发）；`ApprovalRequest` 与任务暂停的自动闭环（当前 `approval_wait` 仅发事件并暂停）。
