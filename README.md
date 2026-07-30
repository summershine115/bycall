# Bycall

> **企业级数字员工 Agent** —— 常驻企业微信/飞书等 IM 的企业级数字员工，前期以 RAG 业务知识问答为主，后续扩展 Skill / MCP / 多能力。
>
> 路线 B（分层混搭自研）：LangGraph 编排 + LangBot IM 层 + zvec 向量库 + PostgreSQL 记忆/元数据 + MCP 网关。

> **当前版本：v0.2.0 / M6 已完成** —— 已具备治理脊柱、多用户会话、混合检索、
> LangGraph + MCP/HITL，以及企微主动推送、多模态附件、来源卡片、模板卡片、可靠重连和
> 连接可观测。版本新增功能、修复项和升级说明见 [`CHANGELOG.md`](CHANGELOG.md)。
>
> 完整设计见 [`docs/product-design.md`](docs/product-design.md)（产品形态/治理架构/信任模型），
> 后续方向与行业优先级见 [`docs/product-strategy.md`](docs/product-strategy.md)，技术分层与
> 架构红线见 [`docs/architecture.md`](docs/architecture.md)。

---

## 技术栈

| 层 | 选型 | 备注 |
|----|------|------|
| 语言 | Python 3.12 | |
| Web 框架 | FastAPI | 异步，回调+长连接+流式 |
| Agent 编排 | LangGraph | ReAct 协调层 + Function Calling 叶子节点（M3 接入）|
| 向量库 | zvec（起步） | `VectorStore` 抽象，预留 Qdrant/pgvector 迁移 |
| 关系库 | PostgreSQL + pgvector | 状态/记忆/元数据 |
| LLM | OpenAI 兼容 / GLM | 模型无关抽象 |
| 重排 | rerank 模型（硅基流动 / Jina / 自定义）| 混合检索向量+FTS+filter → RRF → rerank（M2）|
| 部署 | Docker Compose → K8s | 内网无公网 |

## 快速开始

```bash
# 1. 建虚拟环境（需 python3.12）
make venv && source .venv/bin/activate

# 2. 安装依赖（运行时 + dev）
make install

# 3. 复制配置
cp .env.example .env   # 按需改 LLM key 等（M0 不强求真实 key，测试用 mock）

# 4. 起 API（热重载，端口 8000）
make dev
# → http://localhost:8000/health  返回 {"status":"ok"}
# → http://localhost:8000/ready   返回版本 + 后端选型
# → http://localhost:8000/docs    Swagger UI

# 5. 跑测试
make ci-local          # lint + unit test（全部本地可跑，无需外部服务）

# 6.（可选）起 postgres + redis
make docker-up
```

> 测试用真实 zvec（嵌入式，无需起服务）+ FastAPI TestClient，因此 `make ci-local`
> 不依赖 postgres/redis/docker，开箱即跑。

## 里程碑进度

- [x] **M0** 基础设施搭建（骨架 + VectorStore + LLM 抽象 + Docker + CI）
  - 项目骨架、配置管理、CI、文档 ✅
  - `VectorStore` 抽象 + zvec 实现（CRUD + 混合检索 + ACL 预过滤）✅
  - 模型无关 LLM 抽象（OpenAI 兼容 / GLM）✅
  - FastAPI 骨架（/health /ready）✅
- [x] **M1** 治理脊柱 + 单渠道 MVP —— 身份/RBAC/权限中间件 + 企微长连接 + 基础 RAG（ACL 预过滤）+ 用户/角色管理 + 基础审计
- [x] **M2** 提示词版本化 + 混合检索增强 —— 提示词/知识库 ACL 管理 + rerank 多服务商 + KB 级角色 ACL
- [x] **M3** Agent 化 —— LangGraph 编排 + 多用户会话（§2.4）+ 分层记忆 + SKILL 系统 + 工具按权限过滤
- [x] **M4** MCP 工具网关 + HITL 审批 —— MCP 注册/发现 + capability token + 破坏性操作人审闭环
- [~] **M5** 生产化 —— OTel/Prometheus/结构化日志等核心可观测能力已落地；合规报表与迁移准备继续演进
- [x] **M6** 企微渠道增强 —— 主动推送、可靠重连、真实连接状态、多模态附件、引用卡片和非破坏性模板卡片交互

## 目录结构

```
src/bycall/
├── config.py          # pydantic-settings 分层配置（DB > .env > 默认）
├── governance/        # ★ 治理脊柱：身份/角色/权限矩阵/能力集（贯穿控制面+数据面）
├── admin/             # ★ 控制面 REST API + schemas（用户/角色/提示词/模型/知识库/SKILL/MCP/审计/审批）
├── agent/             # 数据面执行：runner（治理链路单一真相源）+ im_handler（IM 消息）
├── gateway/           # IM 渠道抽象 + 企微长连接（ChannelManager 单例，热重载）
├── rag/               # 检索 + 拼 prompt + 入库（per-KB 切块，混合检索 RRF）
├── mcp/               # ★ MCP 工具网关（红线 #5：agent 不直连 MCP server）
├── tools/             # 工具注册表 + 治理（filter_tools 白名单 + 审批门）
├── memory/            # 分层记忆（工作/情景/语义/治理，红线 #4）
├── llm/               # 模型无关抽象（chat / embed / rerank，多 provider）
├── vector_store/      # VectorStore 抽象 + zvec 实现（CRUD + 混合检索 + ACL 预过滤）
└── api/               # FastAPI 路由（/health /ready /chat + admin + dataplane）

tests/
├── unit/              # 单元测试（真实 zvec，覆盖治理/工具/MCP/SKILL/审批/限流）
└── integration/       # 端到端治理链路（TestClient，含 IM/MCP/SKILL/HITL 注入边界）
```

详见 [`docs/architecture.md`](docs/architecture.md) 与
[`docs/decisions/`](docs/decisions/)（ADR）。
