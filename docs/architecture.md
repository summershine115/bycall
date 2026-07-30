# Bycall 架构设计

> 路线 B（分层混搭自研）。完整选型评估与排期见 `digital-employee-plan.md`（项目外部文档）。
> 本文档是 Bycall 仓库内的架构速览，随实现演进更新。

## 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        IM 渠道层 (LangBot)            [M1+]      │
│   企微(长连接) │ 飞书(长连接) │ 钉钉(Stream) │ Web              │
│   统一消息事件抽象 → 统一回复接口 │ 访问控制/限速/敏感词            │
└──────────────────────────┬──────────────────────────────────────┘
                           │ 统一 Message 对象
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Agent 编排层 (LangGraph)        [M3 编排✅]    │
│   StateGraph + ToolNode：工具调用回路（按 allowed_tools 过滤）   │
│   状态: session_store 管历史（渐进式，Checkpointer 留后续）      │
│   会话: Conversation/Message 表，按 (user,channel) 隔离上下文    │
│   模型: per-session 可选(Conversation.model_id) → 默认全局激活   │
│   循环: ReAct 协调层 + Function Calling 叶子节点（混合架构）     │
│   Checkpointer/PostgresSaver 暂不接（见 ADR-0002 三大障碍）      │
└───────────────────────────┬─────────────────────────────────────┘
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│ RAG 检索层       │ │ MCP 工具层 [M4]  │ │ 记忆层 [M3]      │
│ VectorStore 抽象 │ │ 网关✅ Token✅   │ │ 短期:对话窗口    │
│  ├ zvec (M0✅)   │ │  HITL✅          │ │ 长期:Postgres   │
│  ├ Qdrant (迁移) │ │ mcp/+approval   │ │  +pgvector      │
│  └ pgvector      │ │  Client+Manager │ │                 │
│ 权限预过滤        │ │                 │ │                 │
│ 混合检索(向+FTS+  │ │                 │ │                 │
│  filter+RRF)     │ │                 │ │                 │
└─────────────────┘ └─────────────────┘ └─────────────────┘
        │                                       │
        ▼                                       ▼
┌─────────────────────────────────────────────────────────────────┐
│            持久化与基础设施层                                      │
│  zvec 数据目录 │ Postgres(+pgvector) │ Redis │ OTel+Langfuse     │
└─────────────────────────────────────────────────────────────────┘
```

标注 `[M?]` 表示该层在对应里程碑落地。✅ 表示已完成。

## M0 已落地的部分

| 层 | 组件 | 状态 | 位置 |
|----|------|------|------|
| 配置 | pydantic-settings 分层配置 | ✅ | `src/bycall/config.py` |
| LLM | 模型无关抽象（对话=ChatOpenAI / 向量化=OpenAIEmbedModel） | ✅ | `src/bycall/llm/` |
| RAG 检索 | `VectorStore` 抽象 + zvec 实现 | ✅ | `src/bycall/vector_store/` |
| Web | FastAPI 骨架 + /health /ready | ✅ | `src/bycall/api/` |
| 基础设施 | Docker Compose（pg+redis+app） | ✅ | `docker-compose.yml` |
| 工程 | CI（lint/test/build） + pre-commit | ✅ | `.github/` `.pre-commit-config.yaml` |

## 架构红线（不可违背，详见 HANDOVER §4）

1. **权限预过滤**：RAG 文档级 ACL，向量检索**前**注入 metadata filter，不是先召回再过滤。
2. **Session 隔离**：`session_key = (agent_id, channel, tenant_id, user_id)`，多用户并发独立。一个 IM bot 连接服务全组织员工，每人私聊 = 一条独立会话（`Conversation`），上下文按会话隔离，提示词/SKILL/MCP/默认模型全局共享（详见 `product-design.md` §2.4）。
   - **DB 落地**：`Conversation(user_id, channel, model_id?)` + `Message(conversation_id, role, content)`，一个 `(user_id, channel)` 对应一条会话。
   - **per-session 模型**：`Conversation.model_id` 可空外键，空 → fallback 全局激活模型（`Model.is_active`）。默认行为兼容 M1/M2 无状态形态。
3. **混合架构**：LangGraph durable（超步边界持久化）+ 同步 RAG 热路径。
4. **分层记忆**：工作/情景/语义/治理四层，**agent memory ≠ RAG**。
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
用户在企微 @数字员工："上季度华东区销售额？"
  → LangBot 收消息(长连接) → 统一 Message → Agent 层
  → LangGraph 意图识别 + guardrail
  → 规划：查知识库(报表) + 可能调数据查询工具
  → RAG 检索(权限预过滤): 向量+FTS+filter 三合一 + RRF rerank
  → 响应生成(带 citation)
  → 记忆更新(episodic → Postgres)
  → LangBot 回复企微
  → 全链路 OTel trace + Langfuse
```

## 后端可切换设计

`VectorStore` 抽象接口定义在 `vector_store/base.py`，zvec 是第一个实现。
迁移触发条件（任一满足）：
- 规模超千万级 chunk → Milvus
- 需原生 RBAC + 复杂权限 → pgvector / Qdrant
- 多 Agent 实例共享 + 高并发 → Qdrant / Milvus

迁移时新增一个实现类，在 `factory.py` 注册即可，业务代码不变。
详见 `docs/decisions/0001-vectorstore-abstraction.md`。

### 文档解析引擎可切换

解析引擎是按扩展名路由的解析器（`rag/parsers/registry.py`，每类型一个实现）**之上**
的策略层，参考 `VectorStore` 的可切换后端设计。引擎抽象在 `rag/parsers/engines.py`：

- `builtin`（默认）：委托给按扩展名路由的本地解析器（MuPDF / python-docx / bs4 / Markdown / Text），零外部依赖。
- `markitdown`：Microsoft MarkItDown 通用解析器（本地库，`pip install 'bycall[parse]'`），一个引擎覆盖 pdf/docx/pptx/xlsx/html/图片等。
- `mineru`：MinerU 远程 API（布局识别 + OCR，适合扫描件 / 复杂版式 PDF）。两种部署由 `mineru_mode` 切换：`selfhost`（自建 mineru-api，`/tasks` multipart 协议）/ `cloud`（mineru.net 精准解析，`/api/v4` 预签名上传 + 轮询协议）。

路由规则（`parse_document`）：引擎不支持该类型 → 自动退回 `builtin`（小众格式不强制走所选引擎）；显式选中的引擎不可用（依赖未装 / 远程未配置）→ 报错而非静默回退。配置走 `BYCALL_PARSE_*`，上传端点支持 `engine` 参数按文档覆盖，`GET /api/admin/parse-engines` 列出引擎及可用性。

**解析异步化**（参考 WeKnora）：上传只做廉价同步校验（扩展名 / 引擎可用性 / 大小），**解析与入库都在后台线程池并行**——多文件并发上传互不阻塞（慢引擎如 mineru 不会卡住其他文件）。解析阶段计入入库进度（`/progress` 的 `stage=parsing`），解析失败/空内容落 `status=failed`（不再立即 4xx），仅「引擎不可用」这类配置错误在请求线程即时 4xx。详见 `docs/decisions/0002-parse-engine-abstraction.md`。
