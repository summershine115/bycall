# AGENTS.md — Bycall workspace instructions for ZCode agents

> 本文件是未来 ZCode agent 会话开工前必读的项目指令。保持简洁、可执行。

## 0. 先读文档（强制）

**动手写任何设计或代码之前，先读 `docs/`：**

- `docs/product-design.md` — 产品形态、治理架构、信任模型、里程碑路线图（§6）、待确认假设（§7）。**决定"做什么、为什么这么做"。**
- `docs/architecture.md` — 分层架构、架构红线（不可违背）、核心数据流、后端可切换设计。**决定"怎么搭、边界在哪"。**
- `docs/decisions/` — ADR（架构决策记录），**决策登记册（唯一入口）是 `docs/decisions/README.md`**：ADR 索引、待拍板清单、新决策流程。新决策先写 ADR 并登记，待拍板项集中在登记册 §3，正文只留指针。
- `docs/execution-plan.md` — **当前执行主方向**：先收口六条治理缝隙 + M5 → 评测/发布门 → 行业试点，冻结 S3/S4。所有新任务对齐其阶段与任务编号（T0.x–T3.x）；未列入冻结清单的新能力不得动工。

新增功能/改架构时，**先确认与文档是否一致**；若文档已落后于代码，先更新文档再改代码。路线图以 `docs/execution-plan.md` 为**当前主方向**（取代默认的 M7+/S3-S4 堆能力），按阶段推进、出口门不达标即停在当前阶段；里程碑历史沿革见 product-design.md §6。
所有决策（含待拍板项）以 `docs/decisions/README.md` 登记册为准，不再散落于正文文档。

## 1. 这是什么项目

企业级数字员工 Agent —— 常驻企微/飞书等 IM，员工通过 IM 协作，但人格（提示词）/能力（工具）/知识（RAG）**由组织管理员管控**，LLM 无法越权。路线 B（分层混搭自研）：LangGraph + LangBot IM + zvec + PostgreSQL + MCP 网关。

核心原则（贯穿全文）：**策略绝不能活在提示词层。** LLM 只能"请求"，中间件/网关/检索层"判定"是否放行。

## 2. 常用命令

```bash
make install          # 装依赖（editable + dev）到当前环境
make dev              # 起 API（热重载 :8000，PYTHONPATH=src）
make ci-local         # 本地模拟 CI：lint + 全测试 + eval + eval-security（无需外部服务）
make test-unit        # 仅单元测试
make test-integration # integration 目录测试（当前使用临时依赖/mock，无需 docker）
make eval             # 四类评测集（知识质量/权限安全/执行质量/运营效果）
make eval-security    # 权限攻击红线用例（五类攻击 × ≥3，任一失败即红灯）
make lint             # ruff check
make fmt              # ruff format + check --fix
make docker-up        # 起 postgres + redis（zvec 嵌入式无独立服务）
```

前端（`frontend/`）：`npm run dev` / `npm run build`（Vite + Vue 3 + Element Plus）。

- Python 固定 `3.12`。`make ci-local` 全本地可跑（zvec 嵌入式 + TestClient，不依赖 docker）。
- 单测用真实 zvec（`tmp_zvec_dir` fixture），API 测用 FastAPI TestClient。

## 3. 架构边界（改代码前必须遵守）

**代码探索首选：** 修改任何代码前，必须先调 `codegraph_explore`（`xd://mcp__codegraph_explore`）一次性获取相关符号的源代码 + 调用链 + 影响范围（blast radius）。这是默认路径——grep / Read / LSP 仅在 codegraph 无法覆盖的场景（配置文件、非可解析文本、宽泛全文搜索）降级使用。

两个面 + 一条脊柱（详见 architecture.md「架构红线」）：

- **控制面**（`admin/` + 前端）：admin 定义规则（用户/角色/提示词/模型/审计）。用户的请求永远进不了这里。
- **数据面**（`api/dataplane.py` REST + `agent/im_handler.py` IM）：员工交互执行层。
- **治理脊柱**（`governance/`）：身份 → 角色 → 权限矩阵 → 能力集。**所有功能的先决条件**，不是补丁。

关键边界规则：

- **权限预过滤**：RAG 检索**前**注入 metadata filter，不是先召回再过滤（红线 #1）。
- **Session 隔离**：`session_key = (agent_id, channel, tenant_id, user_id)`；一个 bot 服务全组织，每人私聊独立会话（红线 #2，详见 product-design.md §2.4）。
- **控制面/数据面共用 `run_agent`**（`agent/runner.py`）——治理脊柱的单一真相源，差异只在回复方式。
- **LLM 是执行单元**：提示词、工具、知识在被 LLM 看到**之前**就已被治理脊柱过滤。

包职责速查：`governance/`（身份/权限/模型实体）、`admin/`（控制面 API+schemas）、`agent/`（runner 治理链路 + im_handler）、`gateway/`（IM 渠道抽象 + 企微）、`rag/`（检索+拼prompt）、`llm/`（模型抽象）、`vector_store/`（向量库抽象+zvec）、`api/`（FastAPI 路由）。

## 4. 编码约定

- Python 3.12，ruff（line-length 100，double quotes，target py312）。`make fmt` 后再提交。
- **注释用中文**（项目惯例；ruff 已 ignore 全角标点告警 RUF001/002/003）。模块/函数 docstring 说清"为什么"。
- SQLModel ORM：`table=True` 实体在 `governance/models.py`；JSON 列用 `governance/json_types.py`（`PydanticJSONType` / `DictJSONType`），不要用裸 `JSON`。
- DB 访问：数据面用 `session_scope()`（`db.py`），控制面用 FastAPI `Depends(get_session)`。IM 每条消息独立 session。
- `get_settings()` 带 `lru_cache`，测试里每个用例后必须 `get_settings.cache_clear()`（见 `conftest.py`）。
- 运行时配置分层：`.env` 只留启动引导（DB DSN / session_secret / env），其余进 `SystemSetting` 表（DB > .env > 默认）。

## 5. 关键现状（避免误判）

- **当前已启用基础多轮会话**：`run_agent` 按 `conversation_id` 加载历史、摘要和管理员维护的用户记忆。企微群聊在群成员共同授权模型落地前一律拒绝，避免私聊/群聊串历史和群内越权披露。
- **模型全局激活唯一**：`Model.is_active` 同类型只能一个；`get_chat_model(session)` 全系统共用。per-session 模型（`Conversation.model_id` 可空外键）是 M3 预留。
- **IM 渠道单例 + 热重载**：`gateway/manager.py` 的 `ChannelManager` 单例；admin 改凭证后 `reload()` 只重启那一个渠道，HTTP 服务不动。

## 6. 提交规范

- **版本记录强制维护**：用户可见功能、行为变化、安全修复和重要缺陷修复必须在同一任务中
  更新根目录 `CHANGELOG.md` 的 `[未发布]`；纯格式/内部重命名无需记录。正式发布时将条目
  移入对应版本并填写日期，同时同步 `pyproject.toml` 与 `src/bycall/__init__.py` 的版本号，
  创建 `v<版本号>` Git tag。
- commit message 用中文，简述本次改了什么、为什么（参考近期 commit 风格）。
- **改代码前先用 `git worktree` 创建独立工作目录**（而非在当前目录切分支），
  便于并行开发：
  ```bash
  # 从 main 创建新 worktree（自动建分支并 checkout）
  git worktree add -b <feature-name> ../bycall-<feature-name>
  cd ../bycall-<feature-name>
  ```
  所有 worktree 放在主仓库同级目录下（`../bycall-*`），共享同一份 `.git`，
  创建仅需几秒，无额外 clone 开销。
- 任务完成并验证通过后**主动 commit 并合并回 main**——无需等用户要求。线性分支用
  fast-forward；较大特性可用 `--no-ff` 配 `merge:` 前缀（参考 `merge: M4 Slice C` 风格）
  保留分支拓扑。合并后在主仓库执行 `git worktree remove ../bycall-<feature-name>`
  清理 worktree（`git worktree prune` 清理残留）。当前仓库无远程，合并到 main
  即完成集成；将来若配置 remote，push 默认随合并执行，但高风险改动（DB 迁移 /
  删数据 / 公开 API 破坏性变更）先停下确认。
