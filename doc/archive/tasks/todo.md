# S1 任务执行 · 任务清单 ✅

> 状态：历史实施记录（归档，2026-10-13）。原任务勾选与设计描述保留历史，不代表当前待办；当前执行范围以 [执行计划](../../execution-plan.md) 为准。

> 关联：`doc/archive/tasks/plan.md`、`doc/specs/agent-framework-upgrade.md` §3/§8
> 状态：**已完成**（S1a 6/6 + S1b 6/6，全量 CI 绿）

## S1a 执行核心（已完成）

### Phase 1：基础
- [x] **T1** Task/Step/Run 数据模型 + 建表 + 单测（`49f5e61`）
- [x] **T2** typed event 定义 + 事件总线 + 单测（`bcfa3ce`）

### Phase 2：引擎
- [x] **T3** Step 执行器（run_tool_loop 内核 + 重算能力集）（`ae39f8f`）
- [x] **T4** 任务状态机 + 编排引擎（`ca23e35`）

### Phase 3：触发与续跑
- [x] **T5** 对话触发识别 + 接入 run_agent（零回归）（`fa62e7c`）
- [x] **T6** session 续跑 + 共享 event 管线（`147a440`）

## S1b 触发与可视化（已完成）

### T7：TaskSchedule 实体 + admin CRUD
- [x] `TaskSchedule`（cron/event 二选一、执行身份 user_id、goal 模板 `{payload.xxx}` 插值）
- [x] `/api/admin/task-schedules` CRUD（cron 表达式校验、重名 409）+ 手动 fire 端点

### T8：调度器 + 事件入口
- [x] `agent/scheduler.py`：进程内 cron 调度器（单例线程，tick 间隔可配 `task.scheduler_tick_seconds`）
- [x] `POST /api/events/{event_source}`：外部事件触发（event_source 即 webhook key，缺字段 400）

### T9：启动接入
- [x] app 启动自动 `resume_pending_tasks`（后台线程，不阻塞启动）
- [x] 启动/关闭接入任务调度器

### T10：LLM 自动拆多步 plan
- [x] `plan_task_goal`：LLM 拆计划（建议性）→ 代码层校验 kind 白名单；解析失败降级单步
- [x] 对话触发、调度触发共用同一拆解逻辑

### T11：任务审批自动闭环
- [x] `ApprovalRequest` 挂 Task/Step（task_id/step_id 列 + 幂等迁移）
- [x] tool_loop 内工具拦截 → Step `awaiting_approval` + 暂停；`approval_wait` 步创建任务级审批（`__task_approval__`）
- [x] approve → 任务自动恢复执行；reject → 对应步失败 → 任务失败

### T12：任务列表 / 详情 / SSE
- [x] `GET /api/admin/tasks`（状态/用户过滤）+ `GET /api/admin/tasks/{id}`（详情含 steps/runs）
- [x] `GET /api/admin/tasks/{id}/events` SSE：先回放 DB 快照再增量追平，断开即退（心跳 15s）

### T13：前端任务进度页
- [x] `Tasks.vue`：任务列表 + 进度详情（SSE 实时刷新）+ 调度管理 tab（新建/编辑/启停/触发一次）

## 后续（S2 / S3 / S4）
- **S2 记忆层**：`UserMemory` 扩展 + agent 可写路径（保守策略）+ 注入分层
- **S3 skill 演化**：`SkillProposal` + 自动提议 + admin review 晋升 + Skill 版本化
- **S4 推广分发**：晋升 Skill 按角色分发 + 评测门
- 缝隙 #6 对话维度 event 统一（任务维度已经 default_bus 闭合）
- `clarify` 步的用户澄清交互闭环（当前暂停等对话续跑）
