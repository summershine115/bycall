# Implementation Plan：S1a 执行核心（Agent 框架升级第 1 切片）

> 关联：`docs/specs/agent-framework-upgrade.md` §3/§8（S1a）、ADR-0005
> 范围：仅 S1a 执行核心。S1b（触发与可视化）/ S2 / S3 / S4 见 spec §8，各自独立 plan。

## Overview

把 bycall 从「单次对话工具回路」升级为「持久化多步任务」：引入 `Task/Step/Run` 三层实体 +
状态机，让 Agent 能处理「给定目标 → 拆步 → 逐步执行（含 HITL 暂停）→ 可恢复完成」的业务任务。
`run_agent` 的工具回路成为 **Step 的执行内核**（不重写）。同步把 `run_agent` / `run_agent_stream`
收敛到同一 typed event 管线（闭合实现缝隙 #6）。

## Architecture Decisions

1. **`run_tool_loop` 作 Step 内核**：不重写工具回路。Step 执行器 = 拼消息 + 重算能力集 +
   调 `graph.run_tool_loop` + 采审计。
2. **typed event 统一两套 run 路径**：定义事件类型 + 事件总线，`run_agent` / `run_agent_stream`
   都产出同一事件流 → 审计 / 流式输出 / （S1b）前端。解决两套流程漂移（缝隙 #6）。
3. **每步执行前重算 `CapabilitySet`**：复用 `execute_approved_tool` 的已验证模式（重算能力集 +
   白名单验证 + 脱敏），抽成共享函数。满足 ADR-0005 红线。
4. **对话触发**：`run_agent` 识别到「多步任务目标」→ 创建 Task。无识别时退化为当前 `run_agent`
   （零回归）。
5. **状态机驱动续跑**：Task/Step 状态显式持久化，`paused`（等审批/等输入）可跨进程恢复。

---

## Task List

### Phase 1：基础

#### T1：Task/Step/Run 数据模型 + 建表
**Description**：在 `governance/models.py` 新增 `Task/Step/Run`（字段见 spec §6.1），复用现有
建表机制（与 `ApprovalRequest` 同）。

**Acceptance**：
- [ ] 三实体可 CRUD，字段齐全（Task 含 `trigger/schedule_id/status/capability_snapshot_json`，
      Step 含 `kind/idempotency_key/retry_count`，Run 含 `input/output/duration/tool_calls`）
- [ ] `select_by_*` 查询方法（按 task_id 列 Step、按 user_id 列 Task）

**Verify**：`make test-unit`（新增 `test_task_models.py`）绿。
**Dependencies**：None。
**Files**：`governance/models.py`、`tests/unit/test_task_models.py`。
**Scope**：S（2 files）

#### T2：typed event 定义 + 事件总线
**Description**：新增 `agent/events.py`，定义 `TaskEvent` 类型（spec §3.3）+ 轻量事件总线
（发布/订阅，进程内）。

**Acceptance**：
- [ ] 事件类型覆盖 `task_created/plan_produced/step_started/tool_called/approval_requested/
      step_completed/step_failed/task_done/task_paused`
- [ ] 总线支持订阅 + 发布，带 `trace_id`（接 OTel）
- [ ] 可被 `run_agent` / `run_agent_stream` 共用（接口稳定，不绑死某一路径）

**Verify**：`make test-unit`（新增 `test_events.py`）绿。
**Dependencies**：None。
**Files**：`agent/events.py`、`tests/unit/test_events.py`。
**Scope**：S（2 files）

### Checkpoint：基础就绪
- [ ] 数据模型可 CRUD；事件总线可发布/订阅
- [ ] `make ci-local` 绿

---

### Phase 2：引擎

#### T3：Step 执行器（run_tool_loop 内核 + 重算能力集）
**Description**：新增 `agent/task_engine.py` 的 Step 执行器：拼消息 → 重算 `CapabilitySet`
（抽 `execute_approved_tool` 的重算逻辑为共享函数）→ 调 `run_tool_loop` → 产 `Run` + 事件。

**Acceptance**：
- [ ] 执行一个 `kind=tool_loop` 的 Step，产 Run（含 tool_calls）+ `step_started/completed` 事件
- [ ] 执行前重算能力集；用户权限已收回 → 该步被拒（重算生效，可测）
- [ ] 工具结果按 redactor 脱敏（复用现有脱敏）

**Verify**：`make test-unit` + 集成测试：权限变更后 Step 被拒。
**Dependencies**：T1、T2。
**Files**：`agent/task_engine.py`、`tools/approval.py`（抽共享重算）、`tests/unit/test_step_executor.py`。
**Scope**：M（3 files）

#### T4：任务状态机 + 编排引擎
**Description**：在 `agent/task_engine.py` 实现状态机（`draft→planning→running⇄paused→completed/
failed/cancelled`）+ 多 Step 编排：LLM 产计划（`plan_json`，建议性）→ 代码层校验 → 逐步执行，
遇 `approval_wait`/`clarify` 步暂停。

**Acceptance**：
- [ ] Task 状态按 spec §3.2 流转，非法流转被拒
- [ ] 编排多 Step 顺序执行；遇需审批 Step → `paused` + `approval_requested` 事件
- [ ] 恢复（resume）后从暂停点继续，已 done 的 Step 不重跑（幂等）

**Verify**：`make test-unit`（状态机 + 编排）绿。
**Dependencies**：T3。
**Files**：`agent/task_engine.py`、`tests/unit/test_task_engine.py`。
**Scope**：M（2 files）

### Checkpoint：引擎可用
- [ ] 给定 Task，引擎能逐步执行、遇审批暂停、恢复后完成
- [ ] `make ci-local` 绿

---

### Phase 3：触发与续跑

#### T5：对话触发识别 + 接入 run_agent
**Description**：在 `run_agent` 增加对话触发判定——识别「多步任务目标」（显式「启动任务」指令
优先，LLM 判定兜底）→ 创建 Task 走引擎。无识别时走原 `run_agent` 路径。

**Acceptance**：
- [ ] 用户显式「启动任务：…」→ 创建 Task 并走引擎执行
- [ ] 普通单轮问答 → 走原路径，行为不变（零回归，现有测试全绿）
- [ ] Task 事件写入审计（复用 `_audit`）

**Verify**：`make test-integration`（对话触发）+ 现有问答测试零回归。
**Dependencies**：T4。
**Files**：`agent/runner.py`、`tests/integration/test_task_trigger.py`。
**Scope**：M（2 files）

#### T6：session 续跑 + run_agent/stream 共享 event 管线
**Description**：进程重启 / IM 断线后，未完成 Task 从 `paused`/`running` 恢复；把 `run_agent_stream`
接入 T2 的 event 管线，使两套 run 路径产出一致事件（闭合缝隙 #6）。

**Acceptance**：
- [ ] 模拟进程重启，`paused` 任务可恢复并完成
- [ ] `run_agent` 与 `run_agent_stream` 经同一 event 管线，审计字段一致
- [ ] 流式 IM 回复语义不破坏（现有企微流式测试绿）

**Verify**：`make test-integration`（续跑）+ `make ci-local` 全绿。
**Dependencies**：T5。
**Files**：`agent/runner.py`、`agent/im_handler.py`、`tests/integration/test_task_resume.py`。
**Scope**：M（3 files）

### Checkpoint：S1a 端到端
- [ ] IM 对话触发多步任务 → 执行 → HITL 暂停 → 恢复 → 完成，全程审计一致
- [ ] 现有问答路径零回归（`make ci-local` 全绿）
- [ ] Review with human before S1b

---

## Risks and Mitigations

| 风险 | 影响 | 缓解 |
|------|------|------|
| `run_agent` 改造破坏现有问答 | 高 | 零回归优先：无 Task 走原路径；TDD 每步验证现有测试全绿 |
| 状态机 + 续跑复杂度高 | 中 | 先对话触发（同步路径），续跑后置（T6）；幂等键防重复执行 |
| event 管线重构波及流式 IM 回复 | 中 | `run_agent_stream` 增量改造，保留现有逐 token 流式语义 |
| 对话触发误判（单轮问答被当任务） | 中 | 显式「启动任务」指令优先 + LLM 判定兜底；保守判定降低误判 |

## Open Questions

- 对话触发的判定：用户显式「启动任务」指令为主，LLM 自动判定多步目标为辅？建议两者都支持、
  显式优先（降低误判）——实现 T5 时定。
- Task 与 Conversation 的关系：一个 Task 挂一个 Conversation，还是可跨多个？S1a 先 1:1
  （Task 关联当前 conversation_id），跨会话续跑留 S1b/后置。
