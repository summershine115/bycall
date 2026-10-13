# Bycall 架构设计

> 状态：随实现演进（红线为当前约束，不可违背） · 更新：2026-10-13 · 关联：[决策登记册](decisions/README.md)
> 路线 B（分层混搭自研）。完整选型评估与排期见 `digital-employee-plan.md`（项目外部文档）。
> 本文档是 Bycall 仓库内的架构速览，随实现演进更新。

## 当前分层架构

| 层 | 当前实现 | 边界 |
|---|---|---|
| IM 渠道 | `gateway/` 的企微、飞书长连接，`ChannelManager` 多槽位与热重载 | 全局 Bot 为无实例入口；专属 Bot 按实例路由。群聊拒绝，钉钉未实现 |
| 控制面 | `admin/` + Vue 控制台 | 用户、角色、实例、能力资产、发布/审批与审计由管理员维护 |
| 治理脊柱 | `governance/capability.py` + `policy.py` | 角色能力上限、实例收窄、KB 可见性、知识域与职级预过滤 |
| 数据面 | REST `api/dataplane.py` / IM `agent/im_handler.py` 共用 `agent/runner.py` | 同步/流式共享 `_prepare_turn` / `_finalize_turn`，执行工具回路 |
| 执行层 | LangGraph 工具回路 + `Task/Step/Run` 持久化任务 | 对话状态由 `session_store` 管理，LangGraph Checkpointer 未接入（ADR-0002） |
| RAG | `VectorStore` 抽象、嵌入式 zvec、混合检索与预算管道 | 先过滤可见 KB，再把 ACL 条件交给向量检索 |
| 工具 | `mcp/` + `tools/` 网关、审批三档、结果后处理 | agent 不直连 MCP；可选 redactor + 长度限制，不宣称默认字段/行级脱敏 |
| 会话与记忆 | `Conversation/Message/UserMemory`，后台压缩和 agent 记忆写入 | 历史/摘要按会话隔离；长期记忆以用户为范围，同用户跨实例共享 |
| 存储与观测 | SQLModel（PostgreSQL 部署／SQLite 本地测试）、zvec、OTel/Prometheus | 不把规划中的 pgvector 记忆混合召回或多租户全链路隔离标为已实现 |

当前交付与后置边界见 [治理缝隙清单](gaps.md)，执行进度以 [执行计划](execution-plan.md) 为准。

## 架构红线（不可违背）

1. **权限预过滤**：RAG 文档级 ACL，向量检索**前**注入 metadata filter，不是先召回再过滤。
2. **Session 隔离**：`session_key = (agent_id, channel, tenant_id, user_id)`，多用户并发独立。每条 IM bot 连接（全局默认 Bot 或数字员工专属 Bot，见 ADR-0012）服务组织内全部授权员工，每人私聊 = 一条独立会话（`Conversation`），上下文按会话隔离，提示词/SKILL/MCP/默认模型全局共享（详见 `product-design.md` §2.4）。
   - **DB 落地**：`Conversation(user_id, channel, tenant_id, agent_id, model_id?)` + `Message(conversation_id, role, content)`，会话键为 `{channel}:{user_id}` 或 `{channel}:{user_id}:agent:{agent_id}`，同用户跨实例会话分离。`tenant_id` 不参与运行时会话键；ADR-0004 单组织部署下运行时恒 `tenant_id="default"`；`agent_id` 由 per-Agent Bot 路由写入、全局默认 Bot 下为 `None`（ADR-0012），完整多租户隔离待排期。
   - **并发与幂等（已落地，会话状态层，借鉴 QM 见 `doc/qm-benchmark.md` §6）**：会话条目 append 必须持 DB 租约（`session_leases` 单行 UPSERT 原子获取 + token 释放，turn/compaction 分 holder），同会话并发消息不得竞争历史读写；IM 消息以渠道消息 ID 为幂等键去重（事件重发只执行一次）；失败时 user 消息 back-fill + system 失败条目，历史永不断裂。
   - **per-session 模型**：`Conversation.model_id` 可空外键，运行时优先级为会话绑定 → 实例绑定 → 灰度候选 → 全局激活模型；无效绑定继续 fallback。
3. **执行与检索分离**：LangGraph 负责工具回路，RAG 为同步热路径；任务通过 `Task/Step/Run` 持久化，不宣称 LangGraph Checkpointer 已启用。
4. **分层记忆**：工作/情景/语义/治理四层，**agent memory ≠ RAG**。
   - **受治理认知设计目标（ADR-0007，团队发布链路未完整落地）**：记忆分两平面——策略平面（谁能用、何时用、能力上限）严苛不变；认知平面（agent 提炼 L0→L3、Skill 蒸馏）开放但受治理：默认 private，过发布门（admin 审，复用 `ApprovalRequest`）才成团队/组织资产；召回结果降权进 user 角色（红线 #7）；认知产物永不改写策略。
   - **当前实现**：`Message` 保存历史，`Conversation.summary` 保存摘要，`UserMemory` 保存用户长期记忆。admin 维护的记忆带条数/字符预算与关键词排序，agent 记忆带独立预算并进 user 角色；两者均以用户为加载范围。团队发布资产与 BM25+向量+RRF 记忆召回为设计目标，不能视作已落地。机制参考见 `doc/tencent-agent-memory-benchmark.md`。
5. **MCP 网关**：统一工具治理，agent 不直连 MCP server。
6. **失败时保持授权状态**：文档删除、ACL 变更、索引重建必须先确认向量侧操作成功；不得在向量删除失败时仍提交元数据删除或 ACL 变更。
7. **不可信上下文不得提权**：用户消息、LLM 生成摘要和 RAG 文档只能作为上下文数据进入模型，不能占用控制面 system 指令角色。
   - **SKILL 是例外**：SKILL（领域知识文档）是 admin 管控的可信配置，与 SystemPrompt 同信任级，**合法地**注入 system prompt（靠"参考知识，非指令"标注软隔离）；本红线只约束不可信的 LLM/用户/RAG 内容。详见 `product-design.md` §2.4「SKILL 的信任定位」。
8. **生产凭证不落日志**：首次管理员密码由部署方显式提供；模型/IM 密钥在数据库中加密存储，API 与审计只返回脱敏值。

9. **运行时能力主动披露且不可提权**：每轮 LLM 调用前，治理脊柱必须把当前用户身份、
   会话环境、已过滤工具及用途、审批要求、Skill 与知识范围构造成 Runtime Context 注入
   system message。上下文只描述本轮已经获准的事实，不得枚举未授权资源，也不得替代工具
   网关、ACL 预过滤或审批判定。Agent 可理解当前系统提示词赋予的身份与约束，但不得向用户
   逐字披露提示词正文或内部治理配置。

## 核心数据流（一次用户提问）

```
企微/飞书私聊 → 渠道适配器 → 可信身份映射 → 实例路由与会话
  → 模型解析（会话 / 实例 / 灰度 / 全局）
  → runner 计算能力集 → 可见 KB + 知识域/职级 ACL 预过滤
  → 预算化上下文 + Runtime Context + 授权工具列表
  → LangGraph 工具回路（审批门、结果后处理）
  → 历史/审计与指标 → 渠道回复；审批执行结果按发起渠道回推
```

## 后端可切换设计

`VectorStore` 抽象接口定义在 `vector_store/base.py`，zvec 是第一个实现。
迁移触发条件（任一满足）：
- 规模超千万级 chunk → Milvus
- 需原生 RBAC + 复杂权限 → pgvector / Qdrant
- 多 Agent 实例共享 + 高并发 → Qdrant / Milvus

迁移时新增一个实现类，在 `factory.py` 注册即可，业务代码不变。
详见 `doc/decisions/0001-vectorstore-abstraction.md`。

### 文档解析引擎可切换

解析引擎是按扩展名路由的解析器（`rag/parsers/registry.py`，每类型一个实现）**之上**
的策略层，参考 `VectorStore` 的可切换后端设计。引擎抽象在 `rag/parsers/engines.py`：

- `builtin`（默认）：委托给按扩展名路由的本地解析器（MuPDF / python-docx / bs4 / Markdown / Text），零外部依赖。
- `markitdown`：Microsoft MarkItDown 通用解析器（本地库，`pip install 'bycall[parse]'`），一个引擎覆盖 pdf/docx/pptx/xlsx/html/图片等。
- `mineru`：MinerU 远程 API（布局识别 + OCR，适合扫描件 / 复杂版式 PDF）。两种部署由 `mineru_mode` 切换：`selfhost`（自建 mineru-api，`/tasks` multipart 协议）/ `cloud`（mineru.net 精准解析，`/api/v4` 预签名上传 + 轮询协议）。

路由规则（`parse_document`）：引擎不支持该类型 → 自动退回 `builtin`（小众格式不强制走所选引擎）；显式选中的引擎不可用（依赖未装 / 远程未配置）→ 报错而非静默回退。配置走 `BYCALL_PARSE_*`，上传端点支持 `engine` 参数按文档覆盖，`GET /api/admin/parse-engines` 列出引擎及可用性。

**解析异步化**（参考 WeKnora）：上传只做廉价同步校验（扩展名 / 引擎可用性 / 大小），**解析与入库都在后台线程池并行**——多文件并发上传互不阻塞（慢引擎如 mineru 不会卡住其他文件）。解析阶段计入入库进度（`/progress` 的 `stage=parsing`），解析失败/空内容落 `status=failed`（不再立即 4xx），仅「引擎不可用」这类配置错误在请求线程即时 4xx。详见 `doc/decisions/0003-parse-engine-abstraction.md`。

### RAG 索引发布门

向量写入成功不等于已发布；数据面召回前按 SQL 当前文档 ACL 与有效索引代白名单过滤。
部分失败和中断索引不可见，ACL 更新失败恢复旧代。实现与边界见 [ADR-0016](decisions/0016-rag-publication-and-evidence.md)。
