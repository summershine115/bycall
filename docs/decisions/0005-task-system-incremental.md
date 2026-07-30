# ADR-0005：持久化任务系统（基于 SQLModel 增量，不引独立工作流引擎）

- **状态**：已采纳
- **日期**：2026-07-30（M5）
- **背景**：从单次对话工具循环升级为可恢复的业务任务
- **关联**：`product-strategy.md` §4.3、§3 缝隙 #6、§7、§8 决策点 4、ADR-0002（LangGraph 工具回路）

## 背景

当前执行能力是「单次对话内的同步工具循环」：

- `run_agent` 调 `run_tool_loop`（`src/bycall/agent/graph.py`）：model ↔ tool 循环直到无
  `tool_calls`，取末条 AIMessage 作 answer（`src/bycall/agent/runner.py`）。
- HITL 已落地：`ApprovalRequest`（`src/bycall/governance/models.py`）支持破坏性工具调用的
  人审拦截，审批结果经 IM 主动推送（`src/bycall/tools/approval.py`）。
- `session_store` 管对话历史（`Conversation` / `Message`），但**无持久化的 `Task / Step /
  Run` 概念**——任务随对话结束而消亡，不可恢复、不可跨多步编排。

`product-strategy.md` §4.3 要求从「工具循环」升级为「可恢复的业务任务系统」（分钟级 /
小时级 / 跨天）。附带问题：`run_agent` 与 `run_agent_stream` 两套高度相似流程
（§3 缝隙 #6），策略增多后易行为漂移。

## 决策

基于现有 **SQLModel / `session_store` 增量**建设持久化任务系统，**不引入** Temporal /
Dagster 等独立工作流引擎：

1. 引入 `Task`（一次业务任务）→ `Step`（每一步，确定性状态机）→ `Run`（单次执行尝试）
   三层持久化实体（`src/bycall/governance/models.py`，复用现有表与 session 体系）。
2. 每个 Step 具备：**幂等键 / 超时 / 重试 / 补偿**。
3. 支持：中途审批 / 条件分支 / 人工接管 / 取消 / 恢复；长任务进度与最终结果经 IM 主动推送。
4. 在此过程中把 `run_agent` / `run_agent_stream` 的共享部分（策略准备 / 执行后处理 / 审计）
   抽成统一管线，**闭合 §3 缝隙 #6**。

## 为什么不引独立工作流引擎

- §4.3 明确「LLM 可提计划，但执行 / 重试 / 审批 / 补偿由**代码层工作流**决定」——本质是
  确定性状态机，不是通用工作流引擎。
- 现有 `ApprovalRequest` + `session_store` 是现成基座，增量扩展连贯，治理链路（能力集 /
  审计 / 脱敏）一致。
- 首单（制造业故障诊断、备件查询建工单）是**分钟级任务**，用不上 Temporal 的跨天补偿 /
  分布式编排。
- 引入独立引擎的固定成本：新基础设施（部署 / 运维）、与治理脊柱集成成本高。

## 关键约束（红线级）

- **每次执行前重新计算 `CapabilitySet`**：避免任务创建后用户权限变化仍继续执行（§4.3 明确）。
- **保留完整轨迹**：输入 / 决策 / 工具调用 / 审批 / 输出全程审计落库。
- **LLM 可规划，但不可自主执行破坏性步骤**：是否执行 / 如何重试 / 是否审批 / 如何补偿由
  代码层状态机决定（与红线 #7「不可信上下文不得提权」一脉相承）。

## 何时重新评估（任一满足）

- 出现跨天 / 跨服务长事务，需要复杂补偿编排。
- 任务规模 / 并发超出单库承受。
- 需要分布式 worker / 跨实例调度。

## 后果

**优点**：
- 任务可持久化、可恢复、可审计。
- HITL（`ApprovalRequest`）自然融入多步任务，而非单次工具调用的孤岛。
- 同步 / 流式两套治理链收敛为共享管线（闭合 §3 缝隙 #6）。

**代价**：
- 状态机 / 补偿逻辑自研，复杂编排的可维护性靠克制设计兜底。
- 过渡期须保证现有同步工具回路行为**零回归**。

## 不优先（`product-strategy.md` §7）

在生产治理、评测和任务系统完成前，暂不建设多 Agent 自主协作 / Agent 群聊 / 自由讨论式
协作——这些会稀释「组织授权下的可靠执行」。

## 相关

- 工具回路：`src/bycall/agent/graph.py`（`run_tool_loop` / `GraphResult`）、
  `src/bycall/agent/runner.py`（`run_agent` / `run_agent_stream`）
- HITL：`src/bycall/governance/models.py`（`ApprovalRequest`）、`src/bycall/tools/approval.py`
- 会话层：`src/bycall/agent/session_store.py`
- 战略：`product-strategy.md` §4.3 / §7、§8 决策点 4
