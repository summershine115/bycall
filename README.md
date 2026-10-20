# Bycall

> **企业级数字员工 Agent** —— 常驻企业微信/飞书等 IM 的企业级数字员工，已支持受治理 RAG、Skill、MCP 工具与持久化多步任务。
>
> 路线 B（分层混搭自研）：LangGraph 编排 + 企微/飞书渠道适配层 + zvec 向量库 + PostgreSQL 记忆/元数据 + MCP 网关。

> **当前版本：v0.3.0 / M6 已完成** —— 已具备治理脊柱、多用户会话、混合检索、
> LangGraph + MCP/HITL，以及企微主动推送、多模态附件、来源卡片、模板卡片、可靠重连和
> 连接可观测；v0.3.0 起 Agent 可执行多步任务（调度触发 + 可视化）。版本新增功能、修复项和
> 升级说明见 [`CHANGELOG.md`](CHANGELOG.md)。
>
> 完整设计见 [`doc/product-design.md`](doc/product-design.md)（产品形态/治理架构/信任模型），
> 文档入口见 [`doc/README.md`](doc/README.md)，当前进度见 [`doc/execution-plan.md`](doc/execution-plan.md)，技术分层与
> 架构红线见 [`doc/architecture.md`](doc/architecture.md)。

> **本项目是架构参考实现，非生产就绪软件，不提供任何安全担保。**
> 它展示的治理模式可作为设计参考，不构成对您部署环境的安全承诺。

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

## 这个仓库真正值得看的东西

**策略不在提示词层，而在代码层。** LLM 只能"请求"，判定发生在它够不到的地方——
下面六道闸全部由代码与评测用例锁定，`make eval-security` 让五类攻击 × 21 条红线用例全部失败：

| # | 闸 | 机制 | 代码指针 |
|---|---|---|---|
| 1 | 信任根 | 身份来自 IM 平台令牌 → 用户记录，模型无法"自称是谁" | `agent/im_handler.py`、`gateway/` |
| 2 | 看不到 | 无权工具根本不进工具列表，"看到不让调"都不存在 | `tools/gateway.py`（`filter_tools`） |
| 3 | 调了也过闸 | 执行时能力校验 + 连接器侧 `bycall_role` 复核（跨进程纵深） | `governance/capability.py`、`examples/connectors/` |
| 4 | 召回前过滤 | `build_acl_filters` fail-closed，无权知识**从未成为 prompt token** | `governance/policy.py`、`rag/retrieve.py` |
| 5 | 回流也过滤 | `sanitize_tool_result`：有权工具的返回照样脱敏 + 截断 | `tools/redactor.py` |
| 6 | 跨时间重算 | 审批等待 / 任务暂停后用**最新**用户状态重算，撤权即刻生效 | `governance/capability.py`（`reload_user_and_compute_cap`） |

> 一个你能看着五类攻击全部失败的数字员工——治理平时不可见，攻击失败那一刻才可见。

```bash
make venv && source .venv/bin/activate && make install
make seed-reference && make eval-security
```

**诚实的边界**：这套体系保证的是"越权不可能"，不保证"授权范围内的错误行为不可能"。
confused deputy 与提示注入降质仍是开放难题，对策在审批、审计与评测，不在权限层。

## 快速开始

### 最小体验（零 API key、零 Docker）

不需要外部服务，也不需要模型凭证，clone 下来就能验证核心主张：

```bash
git clone https://github.com/summershine115/bycall.git && cd bycall
cp .env.example .env  # 默认 SQLite，无需 docker / 外部服务
make venv && source .venv/bin/activate
make install
make seed-reference   # 灌入参考场景（示例角色/账号/MCP 连接器，幂等）
make eval-security    # 五类攻击 × 21 条红线用例，任一失败即红灯
```

> 全绿即说明：越权召回、会话/记忆泄漏、提示词注入提权、工具越权、审批绕过五类攻击全部被拦。
> 想跑完整自检（lint + 全部测试 + 四类评测），执行 `make ci-local`。

### 完整应用链路（需要 LLM API key）

前置：仓库已 clone、`.venv` 已建好且依赖已安装（见上一节）。

```bash
# 1. 复制配置（默认 SQLite；生产把 BYCALL_POSTGRES_DSN 换成 Postgres）
cp .env.example .env   # 模型与 IM 凭证由控制台管理

# 2. 起 API（热重载，端口 8000）
make dev
# → http://localhost:8000/health  返回 {"status":"ok"}
# → http://localhost:8000/ready   返回版本 + 后端选型
# → http://localhost:8000/docs    Swagger UI

# 3. 跑测试
make ci-local          # lint + 全部测试 + 四类评测 + 安全评测（全部本地可跑，无需外部服务）

# 4.（可选）体验参考场景（示例 MCP 连接器 + 3 类角色 + demo 账号，见 ADR-0009）
make seed-reference   # 幂等灌入；之后 make dev 会拉起 demo 工单/库存连接器

# 5.（可选）起 postgres + redis
make docker-up
```

> 测试用真实 zvec（嵌入式，无需起服务）+ FastAPI TestClient，因此 `make ci-local`
> 不依赖 postgres/redis/docker，开箱即跑。
> 平台**场景无关**（ADR-0009）：`examples/connectors/` 是可整体删除的参考场景包，
> 用于演示如何把业务系统接成 MCP 连接器并套上治理（白名单 / 高危审批 / 身份透传）。

## 历史里程碑

当前处于阶段 3：自动化参考流程已落地，真机演示与真实使用数据待完成。
阶段 1 按单组织范围收口，完整 ABAC、多租户全链路与字段/行级治理仍后置。
详见 [执行计划](doc/execution-plan.md) 和 [交付边界](doc/gaps.md)。

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
├── gateway/           # IM 渠道抽象 + 企微/飞书长连接（ChannelManager 单例，热重载）
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

详见 [`doc/architecture.md`](doc/architecture.md) 与
[`doc/decisions/`](doc/decisions/)（ADR）。

## License

[MIT](LICENSE) © 2026 summershine115
