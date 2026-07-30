# S1a 执行核心 · 任务清单

> 关联：`tasks/plan.md`（详细计划）、`docs/specs/agent-framework-upgrade.md`

## Phase 1：基础

- [ ] **T1** Task/Step/Run 数据模型 + 建表 + 单测
  - 验收：三实体可 CRUD，字段齐全；`select_by_*` 查询可用
  - 验证：`make test-unit`（`test_task_models.py`）绿
  - 依赖：无 · 文件：`governance/models.py`、`tests/unit/test_task_models.py`
- [ ] **T2** typed event 定义 + 事件总线 + 单测
  - 验收：事件类型齐全（spec §3.3）；总线可发布/订阅，带 trace_id
  - 验证：`make test-unit`（`test_events.py`）绿
  - 依赖：无 · 文件：`agent/events.py`、`tests/unit/test_events.py`

### Checkpoint：基础就绪
- [ ] 模型可 CRUD；总线可用 · `make ci-local` 绿

## Phase 2：引擎

- [ ] **T3** Step 执行器（run_tool_loop 内核 + 重算能力集）
  - 验收：执行 tool_loop Step 产 Run + 事件；执行前重算能力集，权限变更则该步被拒；结果脱敏
  - 验证：`make test-unit` + 集成：权限变更后 Step 被拒
  - 依赖：T1、T2 · 文件：`agent/task_engine.py`、`tools/approval.py`、`tests/unit/test_step_executor.py`
- [ ] **T4** 任务状态机 + 编排引擎
  - 验收：状态按 §3.2 流转；多 Step 编排；遇审批暂停；恢复不重跑（幂等）
  - 验证：`make test-unit`（`test_task_engine.py`）绿
  - 依赖：T3 · 文件：`agent/task_engine.py`、`tests/unit/test_task_engine.py`

### Checkpoint：引擎可用
- [ ] 给定 Task 能逐步执行/暂停/恢复完成 · `make ci-local` 绿

## Phase 3：触发与续跑

- [ ] **T5** 对话触发识别 + 接入 run_agent（零回归）
  - 验收：显式「启动任务」→ 创建 Task 走引擎；普通问答走原路径；事件入审计
  - 验证：`make test-integration`（触发）+ 现有问答测试零回归
  - 依赖：T4 · 文件：`agent/runner.py`、`tests/integration/test_task_trigger.py`
- [ ] **T6** session 续跑 + run_agent/stream 共享 event 管线（缝隙 #6）
  - 验收：重启后 paused 任务可恢复；两套 run 路径审计一致；流式 IM 语义不破坏
  - 验证：`make test-integration`（续跑）+ `make ci-local` 全绿
  - 依赖：T5 · 文件：`agent/runner.py`、`agent/im_handler.py`、`tests/integration/test_task_resume.py`

### Checkpoint：S1a 端到端
- [ ] IM 对话触发多步任务 → 执行 → HITL 暂停 → 恢复 → 完成
- [ ] 现有问答路径零回归（`make ci-local` 全绿）
- [ ] Review 后再进 S1b（触发与可视化）
