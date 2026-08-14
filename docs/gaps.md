# Bycall 治理缝隙清单（Governance Gaps）

> 状态：阶段 0 产出（T0.4） · 更新：2026-08-13 · 关联：`docs/execution-plan.md` §阶段 1、`docs/product-strategy.md` §3
> 六条「治理承诺 vs 实际执行」缝隙。阶段 1 逐条闭合后在此勾掉；闭合前任何一条都不允许对外宣称「已治理」。

## 总览

| # | 缝隙 | 现状（源码定位） | 目标行为 | 任务 | 状态 |
|---|------|------------------|----------|------|------|
| 1 | 完整 ABAC 闭环 | `governance/capability.py` + `rag/retrieve.py`：曾只做 `source IN (...)` 角色标签过滤 | `governance/policy.py` 统一产出 `source IN` + `level <= user_level` | T1.1 | ✅ 已闭合（level/密级；dept 待 User.dept 字段） |
| 2 | 会话键单组织落地 | `governance/models.py`：`Conversation`/`Message` 主要是 `(user_id, channel)` | `(agent_id, channel, tenant_id, user_id)` 显式隔离 | T1.5 | ⬜ 未闭合 |
| 3 | 模型绑定生效 | `Conversation.model_id` 已预留，运行时曾仍取全局激活 | `llm/resolver.py` 新增 `get_chat_model_for_conversation`，api/IM 两路按会话路由 | T1.3 | ✅ 已闭合 |
| 4 | `visible_kb_ids` 语义落地 | `governance/capability.py` 计算 + `agent/runner.py` 检索前过滤 | 已是数据面 KB 级 ACL（runner 检索前过滤）；澄清语义 + 补数据面测试 | T1.2 | ✅ 已闭合 |
| 5 | 工具结果脱敏未平台化 | `tools/base.py`（redactor 字段）+ `tools/gateway.py`：靠单工具声明 redactor | 统一字段分级 + 行级过滤 + 输出大小限制，默认拒绝未声明类型 | T1.6 | ⬜ 未闭合 |
| 6 | 同步/流式治理链未收敛 | `agent/runner.py`（`run_agent` + `run_agent_stream`）+ `api/dataplane.py`：两套相似流程 | 抽共享 pipeline（策略准备→执行→后处理→审计） | T1.4 | ⬜ 未闭合 |

## 逐条明细

### 缝隙 #1：完整 ABAC 未闭环（T1.1）
- **已落地（T1.1，commit 见 git log）**：新增 `governance/policy.py`（`build_acl_filters`）；`Document.level` → chunk ACL → zvec `level` 字段 → 检索 `level <= user_level` 预过滤。检索不再手写 `source IN`。
- **剩余**：`dept` 维度——zvec schema 已建字段，但 `User` 尚无 dept 属性，待数据模型补齐后追加 `dept` 条件。
- **原现状**：`ACLMetadata` 已预留 `tenant_id/dept/level/敏感度` 字段；检索入口主要把 `knowledge_scope_tags` 翻译为 `source IN (...)`，部门/职级/密级尚未真正参与召回前过滤。
- **风险**：越权召回——用户能看到其角色标签之外的、按部门/职级/密级本应隔离的文档。
- **目标**：落地 ADR-0005 的统一 Policy 接口，`evaluate(用户, 资源, 动作, 上下文) → 过滤条件`，检索层只消费 policy 产物。
- **验收**：跨部门+职级+密级文档在召回前即被过滤（不进 LLM、不留痕）。
- **源码**：`governance/policy.py`（新）、`governance/capability.py`、`rag/retrieve.py`、`api/dataplane.py`。

### 缝隙 #2：会话键单组织落地（T1.5）
- **现状**：红线定义 `(agent_id, channel, tenant_id, user_id)`，DB 实际主要是 `(user_id, channel)`。
- **风险**：多租户 / 同租户多 Agent / 组织合并场景下会话与记忆可串扰。
- **目标**：会话、记忆、审批、审计、MCP、缓存均带显式 tenant/agent 隔离键。
- **源码**：`governance/models.py`、`db.py`。

### 缝隙 #3：模型绑定未生效（T1.3）
- **已闭合（T1.3）**：`llm/resolver.py` 新增 `get_chat_model_by_id` / `get_chat_model_for_conversation`；`api/dataplane.py` 与 `agent/im_handler.py` 两路改为「先解析会话 → 再按会话路由模型」，per-session 模型（`Conversation.model_id`）优先，空/失效 fallback 全局激活。补 `tests/unit/test_llm_factory.py::test_per_session_model_preferred`。
- **原现状**：`Conversation.model_id` 是正确预留，但运行时仍取全局激活 chat 模型。
- **风险**：per-session 模型策略、成本、数据驻留无法落地。
- **目标**：模型选择纳入能力集 + 成本/驻留策略，runner 按会话绑定模型路由。
- **源码**：`agent/runner.py`、`llm/resolver.py`、`governance/models.py`。

### 缝隙 #4：`visible_kb_ids` 语义未统一（T1.2）
- **已闭合（T1.2）**：核实 `runner._retrieve_across_kbs` 已按 `cap.visible_kb_ids` 检索前过滤 KB（非 admin 只搜可见 KB）；原「runner 遍历全部 KB」的描述已过时（战略 §3 写于 v0.2.0，代码后续已修）。本任务澄清 `CapabilitySet` docstring 语义（数据面 KB 级 ACL，非仅控制面）+ 补 `tests/unit/test_kb_acl.py` 数据面过滤测试。
- **原现状（已过时）**：`CapabilitySet.visible_kb_ids` 已计算，但数据面 runner 仍遍历全部 KB。
- **风险**：未生效字段被误认为安全边界，产生「已治理」的错觉。
- **目标**：二选一——作为数据面 ACL 接入检索，或更名移出能力集；禁止既不算又不改名。
- **源码**：`governance/capability.py`、`api/dataplane.py`、`agent/runner.py`。

### 缝隙 #5：工具结果脱敏未平台化（T1.6）
- **现状**：结果脱敏依赖单个工具声明 redactor，MCP server 主要靠 capability meta 自行鉴权。
- **风险**：未声明数据类型的工具返回可能带敏感字段直接进 LLM 上下文。
- **目标**：统一字段分级 + 行级过滤 + 输出大小限制，默认拒绝未声明类型。
- **源码**：`tools/redactor.py`（新）、`tools/base.py`、`tools/gateway.py`。

### 缝隙 #6：同步/流式治理链未收敛（T1.4）
- **现状**：`run_agent` 与 `run_agent_stream` 两套高度相似流程，各自采集、易漂移（任务维度已由 default_bus 闭合，对话维度未闭合）。
- **风险**：策略增多后两路行为漂移，审计字段不一致。
- **目标**：抽共享「策略准备 → 执行 → 后处理 → 审计」管线，两路共用。
- **源码**：`agent/pipeline.py`（新）、`agent/runner.py`、`api/dataplane.py`。

## 维护约定
- 阶段 1 每个 T1.x 任务闭合后，把对应行状态改为 ✅ 已闭合，并填闭合 commit。
- 闭合前不得在对外文档/README 中宣称对应能力「已治理」。