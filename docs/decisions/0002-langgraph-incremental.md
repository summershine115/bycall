# ADR-0002：渐进式引入 LangGraph（编排+工具先行，Checkpointer 留后续）

- **状态**：已采纳
- **日期**：2026-07-15（M3 工具治理）
- **背景**：M3 需要工具调用回路（function calling + 按 `allowed_tools` 过滤），architecture.md 原规划 M3 用 LangGraph 编排

## 背景

M3 的核心目标是让治理脊柱的 `CapabilitySet.allowed_tools` 真正被消费——LLM 的工具列表 = 该角色可用工具集，
无权工具 LLM 根本看不到。这需要：

1. 对话模型支持 function calling（`bind_tools` + 解析 `tool_calls`）
2. 工具调用回路（model → tool → model，直到无 tool_calls）
3. 工具网关按 `allowed_tools` 过滤工具列表

architecture.md 规划 M3 用 LangGraph，但当时未确认引入范围。

## 决策

**渐进式引入**：M3 只用 LangGraph 的 **StateGraph + ToolNode** 搭工具回路，用 **langchain-openai 的 ChatOpenAI**
替代自研 `OpenAIChatModel`（原生支持 `bind_tools`），**保留 `session_store` 会话层**（graph 无状态执行 +
session_store 管历史，两者自洽）。Checkpointer / PostgresSaver 留待后续里程碑决策。

## 为什么不一步到位（全面迁移）

调研确认全面迁移到 PostgresSaver Checkpointer 有三大硬障碍：

| 障碍 | 细节 |
|------|------|
| **测试路径分叉**（最致命）| `PostgresSaver` 只支持 PostgreSQL，不支持 SQLite。项目全部测试走 SQLite tmp 文件（262 个测试），"测试=生产路径"的一致性会直接破坏——checkpointer 相关 bug 在测试层不可见 |
| **双 PG driver + 双连接池**| checkpointer 用 psycopg3（要 `autocommit=True`），治理层用 SQLAlchemy。两套连接池、两套事务模型要分别管理 |
| **控制面会话端点失去数据源**| admin 的 `GET /conversations`、`/messages`、`DELETE`、`reset` 依赖对 Conversation/Message 表的结构化 SQL 查询（按用户列表、按时间倒序、分页）。checkpoint 表是 JSON blob，无法支撑这些关系查询。此外 session_store 的滑窗/增量摘要/双条审计语义，Checkpointer 一个都不提供 |

冲击面：约 11-13 个生产文件 + 1255+ 行测试（占总量 23%），其中 session_store(339行) + llm_factory(341行) 几乎整体重写。

## 为什么渐进式可行

M3 不需要 Checkpointer——graph 无状态执行 + session_store 继续管历史，两者完全自洽：

- 历史从 DB 加载 → 拼进 graph 的 initial messages（和现在 `build_conversation_messages` 做法一致）
- graph 跑完（含多轮工具调用）→ 取末条 AIMessage.content 作 answer → `save_message` 写回 DB（控制面/审计投影）

这样：(1) 工具回路（M3 真正目标）拿到手；(2) session_store 测试基本不动；(3) 测试仍全走 SQLite，生产路径一致性不破坏；
(4) 三大风险全部延后到有了明确需求再决策。

## 后果

**优点**：
- M3 工具治理完整闭环，`allowed_tools` 从"空头支票"变成被消费的真实约束。
- 依赖增量小（3 个包），测试冲击接近 0。
- ChatOpenAI 原生支持 OpenAI 兼容协议（覆盖现有 7 个 provider），`bind_tools` 开箱即用。

**代价 / 留给后续**：
- `OpenAIChatModel` / `GLMClient` 标记 deprecated（保留代码降风险，后续清理）。
- graph 暂无持久化（durable/checkpoint/并行能力留后续）。
- Checkpointer 迁移的三大障碍仍在，待控制面会话端点的数据源问题有解后再评估。

## 相关

- 调研依据：LangGraph `PostgresSaver` 官方文档（psycopg3、不支持 SQLite）、checkpoint 表 schema（JSON blob）
- 现有会话层：`src/bycall/agent/session_store.py`
- 工具回路：`src/bycall/agent/graph.py`（本里程碑新增）
- 治理源头：`src/bycall/governance/capability.py` 的 `CapabilitySet.allowed_tools`
