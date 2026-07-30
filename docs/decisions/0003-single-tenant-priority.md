# ADR-0003：单组织私有部署优先（tenant 预留不强制落地）

- **状态**：已采纳
- **日期**：2026-07-30（M5 企业生产化收口）
- **背景**：多租户隔离的部署形态与推进时机
- **关联**：`architecture.md` §架构红线 #2、`product-design.md` §2.4、`product-strategy.md` §8 决策点 2

## 背景

架构红线 #2 定义会话键 `session_key = (agent_id, channel, tenant_id, user_id)`，目标是
一个 IM bot 服务全组织、每人私聊独立会话。但当前 DB 与运行时落地形态是：

- `Conversation(user_id, channel, model_id?)`——一个 `(user_id, channel)` 对应一条会话，
  会话表无 `tenant_id` 列（`src/bycall/governance/models.py`）。
- `CapabilitySet` 无 `tenant_id` 字段（`src/bycall/governance/capability.py`：仅
  `user_id / user_name / role / knowledge_scope_tags / level / ...`）。
- 检索过滤 `_retrieve_across_kbs(session, question, knowledge_scope_tags)` 只把
  `knowledge_scope_tags` 翻译成 `source IN (...)`，不消费 chunk 的 `tenant_id`
  （`src/bycall/agent/runner.py`）。
- 入库时所有 chunk 都打上固定 `ACLMetadata(tenant_id=_DEFAULT_TENANT)`
  （`src/bycall/rag/ingest.py`）——`tenant_id` 是写死的默认值，不是真隔离。

即：多租户的**数据结构部分预留**（`ACLMetadata.tenant_id` 等），但**运行时未消费**，
实质是单组织模型（`product-strategy.md` §2「多租户」一栏原话）。

## 决策

M5 阶段维持**单组织私有部署优先**：

1. 不在 M5 强制落地 `tenant` 维度隔离（会话键、能力集、检索 filter 均不新增 tenant 过滤）。
2. 已预留的 `tenant_id` 字段与红线 #2 的完整 `session_key` **保留不删**，作为未来多租户
   的架构逃生口。
3. 红线 #2 的完整 `session_key` 是**目标态**；本 ADR 明确「当前阶段不落地 tenant 维度」，
   待触发条件满足再启动收口。

## 为什么不现在做多租户

多租户改造是**面最大的工程**——用户 / 角色 / 知识库 / 文档 / 会话 / 记忆 / 审批 / 审计 /
MCP / 缓存，每个实体都要加 `tenant_id`，每条查询都要带 tenant 过滤，每条检索路径都要把
tenant 纳入预过滤。在首单（企业试点）未验证治理价值前投入属于过度工程；单组织形态不
影响当前试点的治理闭环（`product-strategy.md` §2）。

保留预留字段确保未来启动时 **schema 无需大改**（字段已在），只需补消费逻辑 + 全链路
query 加 tenant 过滤。

## 触发条件（任一满足即启动多租户收口）

- 出现 SaaS 多租户客户需求或明确的 SaaS 商业模式。
- 出现多组织合并 / 组织间数据隔离场景。
- 同一租户需要多 Agent 实例（`agent_id` 维度开始被真正使用）。

## 后果

**优点**：
- 单组织部署治理边界天然清晰（无跨租户泄漏面），首单交付快。
- `tenant_id` 预留字段不删，未来启动多租户时是现成接入点。
- M5 工作量聚焦在治理一致性（ABAC / 任务 / 审计），不被多租户改造稀释。

**代价**：
- SaaS 多租户商业模式延后，需等真实需求 + 商业确认（§8 决策点 2）。
- 红线 #2 的 tenant 维度在代码层「名实不符」（预留未消费），必须在注释 / 文档显式标注，
  避免误判「已多租户」。

## 相关

- 红线定义：`architecture.md` §架构红线 #2
- 会话落地：`src/bycall/governance/models.py`（`Conversation`）、
  `src/bycall/agent/session_store.py`（`get_or_create_conversation` / `select_by_session_key`）
- 能力集：`src/bycall/governance/capability.py`（`CapabilitySet`）
- 检索过滤：`src/bycall/agent/runner.py`（`_retrieve_across_kbs`）
- chunk ACL：`src/bycall/vector_store/models.py`（`ACLMetadata.tenant_id`）、
  `src/bycall/rag/ingest.py`（`_DEFAULT_TENANT`）
- 战略判断：`product-strategy.md` §2 / §8 决策点 2
