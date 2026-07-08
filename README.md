# Bycall

> **企业级数字员工 Agent** —— 常驻企业微信/飞书等 IM 的企业级数字员工，前期以 RAG 业务知识问答为主，后续扩展 Skill / MCP / 多能力。
>
> 路线 B（分层混搭自研）：LangGraph 编排 + LangBot IM 层 + zvec 向量库 + PostgreSQL 记忆/元数据 + MCP 网关。

> 🚧 **当前里程碑：M0 基础设施搭建** —— 项目骨架、`VectorStore` 抽象 + zvec 集成、模型无关 LLM 抽象、配置管理、Docker Compose、CI。
> 完整设计与排期见 [`docs/architecture.md`](docs/architecture.md)。

---

## 技术栈

| 层 | 选型 | 备注 |
|----|------|------|
| 语言 | Python 3.12 | |
| Web 框架 | FastAPI | 异步，回调+长连接+流式 |
| Agent 编排 | LangGraph | M3 引入；M0 未用 |
| 向量库 | zvec（起步） | `VectorStore` 抽象，预留 Qdrant/pgvector 迁移 |
| 关系库 | PostgreSQL + pgvector | 状态/记忆/元数据 |
| LLM | OpenAI 兼容 / GLM | 模型无关抽象 |
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
- [ ] **M1** MVP —— 单渠道 + 基础 RAG 对话
- [ ] **M2** 多渠道 + 权限 + 混合检索
- [ ] **M3** Agent 化 —— LangGraph 编排 + Skill 系统
- [ ] **M4** MCP 集成 + HITL 审批
- [ ] **M5** 生产化 —— 可观测 / 高可用 / 迁移准备

## 目录结构

```
src/bycall/
├── config.py          # pydantic-settings 分层配置（LLM/VectorStore/DB）
├── llm/               # 模型无关 LLM 抽象（OpenAI 兼容 / GLM）
│   ├── base.py        #   LLMClient 抽象：chat() / embed()
│   ├── openai_compat.py
│   ├── glm.py         #   复用 OpenAI 兼容协议
│   └── factory.py     #   按 provider 实例化
├── vector_store/      # VectorStore 抽象 + zvec 实现（M0 核心）
│   ├── base.py        #   后端无关抽象接口
│   ├── models.py      #   Chunk / ACLMetadata / SearchRequest ...
│   ├── zvec_store.py  #   zvec 实现（封装 PoC 踩坑约束）
│   └── factory.py     #   按 backend 实例化
├── api/               # FastAPI 骨架（/health /ready）
└── (agent/ tools/ memory/ gateway/ 占位，M1+ 填充)

tests/
├── unit/              # 配置 / LLM / 向量库（真实 zvec）
└── integration/       # API 健康检查（TestClient，无需外部服务）
```

详见 [`docs/architecture.md`](docs/architecture.md) 与
[`docs/decisions/`](docs/decisions/)（ADR）。
