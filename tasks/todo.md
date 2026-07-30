# S1a 执行核心 · 任务清单 ✅

> 关联：`tasks/plan.md`、`docs/specs/agent-framework-upgrade.md`
> 状态：**已完成**（6/6 任务，全量 589 passed 零回归）

## Phase 1：基础

- [x] **T1** Task/Step/Run 数据模型 + 建表 + 单测（`49f5e61`）
- [x] **T2** typed event 定义 + 事件总线 + 单测（`bcfa3ce`）

### Checkpoint：基础就绪 ✅

## Phase 2：引擎

- [x] **T3** Step 执行器（run_tool_loop 内核 + 重算能力集）（`ae39f8f`）
- [x] **T4** 任务状态机 + 编排引擎（`ca23e35`）

### Checkpoint：引擎可用 ✅

## Phase 3：触发与续跑

- [x] **T5** 对话触发识别 + 接入 run_agent（零回归）（`fa62e7c`）
- [x] **T6** session 续跑 + 共享 event 管线（`147a440`）

### Checkpoint：S1a 端到端 ✅
- [x] 对话触发多步任务 → 执行 → HITL 暂停 → 恢复 → 完成
- [x] 现有问答路径零回归（全量 589 passed）
- [ ] Review 后进 S1b（触发与可视化）

## 后续（留 S1b / S2）
- 缝隙 #6 对话维度 event 统一（任务维度已经 default_bus 闭合）
- LLM 自动拆多步 plan（当前为显式「启动任务：」单步）
- TaskSchedule 定时 / 事件触发 + 前端 SSE 任务进度页
- app 启动接入 `resume_pending_tasks`
