# 对标参考：TencentDB Agent Memory（TAM）可借鉴机制

> 调研日期：2026-08-06 · 来源：[github.com/TencentCloud/TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)（MIT）`feat/server_team` 分支
> 定位：腾讯出品的**面向 Agent 团队的记忆系统**——把"凡能让下一个 Agent 少走弯路的信息"统一抽象成
> 记忆资产（Chat Memory / Skill / Wiki / CodeGraph），沉淀→流动→按身份装配（Loadout）给 Agent。
> 本文记录 TAM 中值得 Bycall 借鉴的机制。**与 QM 不同：TAM 的信任模型比 Bycall 宽松**（agent 可自主
> 提炼记忆并进 system 权重），因此每条借鉴项都需"治理化改造"才能落地——本文逐条标注改造点与红线关系。
> 关联决策：`docs/decisions/0006-governed-cognition-two-plane.md`（受治理认知：两平面信任模型）。

## 0. 信任模型差异（决定借鉴方式）

QM 与 Bycall 信任模型同构，机制可直接映射；**TAM 不同**——它的核心卖点是"LLM 自主沉淀经验"，
而 Bycall 的红线是"LLM 不能越权写记忆"。直接照搬会违反红线 #7。但 TAM 解决的问题（agent 如何
继承经验）正是 Bycall 想要的，所以借鉴的关键是**取结构、套治理门**。

| 维度 | TAM | Bycall |
|---|---|---|
| 定位 | 个人/团队 Agent，**让经验自动复利** | 组织数字员工，**策略绝不活在提示词层** |
| 记忆归谁 | LLM 自主提炼、自主写，享 system 权重 | "记忆是组织管控的，不是 LLM 自己写的"（旧立场，见 ADR-0006） |
| 记忆注入 | 记忆可进 system prompt | 不可信内容只能进 user 角色（红线 #7） |
| 知识形态 | 整库可被 agent 按需调用（工具） | RAG 召回结果拼进 prompt |
| 共享边界 | private/team/restricted/agent 四级可见性 | 共享=静态配置；动态数据全隔离（缺第四象限） |

> **一句话**：TAM 的"结构"（分层、有界召回、资产模型、按需知识）几乎全可借鉴；TAM 的"自主性"
> （自由生成 + 自由进 system）全部要套发布门。详见 ADR-0006。

## 1. 核心机制（值得研究的六个点）

### 1.1 记忆逐层生长（L0→L3 异步 Pipeline）

对话不平铺存储，异步提炼成不同粒度，生成与召回都分层：

| 层级 | 保存什么 | 主要用途 |
|---|---|---|
| L0 Conversation | 原始对话与完整上下文 | 核对原话、时间和来源 |
| L1 Atom | 从对话提取的事实/偏好/约束/事件 | 精确召回可执行信息 |
| L2 Scenario | 围绕项目或场景组织的知识块 | 快速恢复一个工作场景 |
| L3 Core / Persona | 长期画像、稳定模式与高层认知 | 让 Agent 迅速进入用户和团队语境 |

工程细节（从插件配置反推）：生成是触发式的（`everyNConversations: 5` 跑 L1→L2，
`persona.triggerEveryN: 50` 跑 L3）；召回是混合的（BM25 + 向量 + RRF，
`maxResults / scoreThreshold`）；**结果有条数 + 字符预算 + 超时三重上限**，防止记忆反噬上下文。

### 1.2 四类资产统一为 Memory Asset + Loadout 装配

Chat Memory / Skill / Wiki / CodeGraph 全部登记为同一种"资产"，通过 **Fixed Binding + ACL**
决定 Agent 带走哪些：先按 Team/User/Agent/可见性缩小权限范围，再按当前问题召回。换 Agent 或换
框架只需重新装配，不必重训。

### 1.3 知识作为按需工具（最值得借鉴的架构抉择）

Wiki（文档→结构化页面 + 链接图谱）和 CodeGraph（代码→符号/文件/调用关系/影响路径）平时**不进
上下文**，agent 通过 `/v3/tools/list` 发现能力、`/v3/tools/call` 按需读取。把"知识检索"从
"拼进 prompt 的噪音"变成"agent 主动调用的工具"。

### 1.4 资产治理：可见性 + 归属 + 版本

四级可见性 `private / team / restricted(ACL) / agent`；每条资产带 Owner、版本、状态、使用次数、
Agent 绑定。**分享是明确动作，不是默认泄漏。**

### 1.5 冷启动：先读档再开工

导入代码库→CodeGraph 自动索引；导入文档→Wiki 自动建图谱；导入历史对话→自动提炼 Skill + Chat Memory。
把"已付过的学习成本"变成存档。

### 1.6 Benchmark 驱动验证

PersonaMem（长期交互后能否正确运用用户信息）从 48%→76%（+59%）。有可量化的记忆有效性度量。

## 2. 可借鉴机制（含治理化改造）

> 改造原则（ADR-0006）：agent 可自由生成认知产物，但 (1) 默认 private；(2) 进 system 前过发布门
> （admin 审，复用 `ApprovalRequest` 模式）；(3) 召回结果降权进 user 角色（红线 #7 不变）；
> (4) 认知产物永不改写策略。

### 2.1 召回预算控制（P0，无红线风险）

**TAM 做法**：召回结果经条数 + 字符 + 超时三重上限，`maxResults / scoreThreshold / strategy=hybrid`。

**Bycall 现状**：`load_user_memory` 返回**全部**用户记忆注入 system message，记忆增长后必然撑爆
上下文，且无相关性排序。

**改造点**：无需治理化改造，纯收益。加 `maxResults` + 字符预算 + 混合排序。**应作为 M3 记忆层
的第一个落地项。**

### 2.2 L0-L3 分层结构 + 混合召回（P0，落实红线 #4）

**TAM 做法**：见 §1.1。平时用 L2/L3 进语境，需细节时 BM25 + 向量 + RRF 回溯 L1/L0。

**Bycall 现状**：`agent/memory/` 是空占位；红线 #4"分层记忆、agent memory ≠ RAG"只是口号。
实际记忆 = admin 手填 `UserMemory` + 压缩 `summary` + 历史窗口。

**改造点**：四层映射——L0=`Message`、L1=`UserMemory`(atom)、L2=场景块、L3=稳定画像。复用现有
zvec/pgvector 做向量召回。**这是 ADR-0006 的落地主体。**

### 2.3 记忆提取的发布门（P0，核心治理化改造）

**TAM 做法**：LLM 自动抽 L1/L2/L3，自主写入。

**改造点（关键）**：**不照搬自动写**。LLM 提议→`pending` 草稿→admin 审核通过→promote。复用 Bycall
已有的 `ApprovalRequest` / HITL 模式。草稿与已发布记忆都降权进 user 角色（红线 #7）。这把 TAM 的
"自主性"转成"受治理的自主性"。

### 2.4 知识作为按需工具（P1，范式级启发）

**TAM 做法**：Wiki/CodeGraph 走 `/tools/list` + `/tools/call`，不整库注入。

**Bycall 现状**：RAG→直接拼 prompt，召回多少塞多少。

**改造点**：把"知识检索"暴露为受治理的内置工具（过 capability check + 审计，复用 §2.1 已落地的
`load_skill` 按需披露模式）。agent 按需调，减少噪音且天然走权限过滤。**与支柱 3 工具治理同构，
不引入新信任模型。**

### 2.5 Skill 结构化升级（P1，强化既有方向）

**TAM 做法**：Skill = 版本 + 资源文件 + 触发边界 + 执行步骤 + 验证规则，非纯 Prompt；生命周期
draft→review→publish。

**Bycall 现状**：Skill 已支持按需披露（`load_skill`）+ 导入导出，但仍偏 markdown 文档。

**改造点**：升级为带版本/触发条件/校验的资产。**与 product-design §8 QM 借鉴的"Skill 生命周期
draft→review→publish"完全一致——TAM 在此点上印证并具体化了既有方向，不新增工作。**

### 2.6 资产统一登记 + 可见性（P2）

**TAM 做法**：四类资产同构，private/team/restricted(ACL)/agent 四级可见性。

**Bycall 现状**：prompt 有版本、memory 无版本、知识无统一登记；共享/隔离图缺"共享动态"象限。

**改造点**：统一资产模型 + 可见性。**强化 QM 的 Workspace Layers（qm-benchmark §2.6）——TAM 的
可见性分级是同一思路的另一种表达，二者合并落地。**

### 2.7 冷启动：历史对话提炼（P2）

**TAM 做法**：导入对话 Session→自动抽 Skill + Memory。

**改造点**：Bycall 可导入历史 IM 对话，提炼组织级 FAQ/Skill。**同样需 §2.3 的发布门**——提炼产物
默认 pending，admin 审后才成组织资产。

### 2.8 记忆 Benchmark（P2）

**TAM 做法**：PersonaMem 量化记忆有效性（48%→76%）。

**改造点**：Bycall 已有 `qm-benchmark.md` 传统与评测习惯，可加记忆层有效性度量，验证分层记忆是否
真的让 agent"更懂用户"。

## 3. 不建议照搬

| TAM 能力 | 不照搬原因 |
|---|---|
| Proxy 注入（CC/CodeBuddy 走代理改 system prompt） | Bycall 不用 proxy，治理脊柱在 `run_agent` 内直接判定；"按身份注入"的思路已由 runner 实现 |
| 跨框架迁移（OpenClaw/Hermes/CC 多宿主） | Bycall 单框架（LangGraph），无此需求 |
| LLM 自主写记忆进 system 权重 | 违反红线 #7；改为受治理草稿（§2.3） |
| MemoryCore 独立 SQLite sidecar 进程 | Bycall 用 Postgres + pgvector，记忆落主库，与审计/会话同库同事务 |

## 4. 落地映射与优先级

| 优先级 | 借鉴项 | TAM 机制 | 落地到 Bycall | 红线关系 |
|---|---|---|---|---|
| P0 | 召回预算控制 | 三重上限（§2.1） | `load_user_memory` 加预算 | ✅ 无冲突 |
| P0 | L0-L3 分层 + 混合召回 | 逐层生长（§2.2） | M3 记忆层主体（红线 #4） | ✅ 红线 #4 本就要求 |
| P0 | 记忆提取发布门 | LLM 自主提炼→**改受治理**（§2.3） | 复用 `ApprovalRequest`，草稿降权进 user | ⚠️ 需治理门，否则违反 #7 |
| P1 | 知识作为按需工具 | `/tools/list`+`/tools/call`（§2.4） | 知识检索暴露为受治理内置工具 | ✅ 与支柱 3 同构 |
| P1 | Skill 结构化升级 | 版本+触发+校验（§2.5） | M3 Skill 系统升级 | ✅ 印证 QM 既有方向 |
| P2 | 资产统一登记 + 可见性 | 四级可见性（§2.6） | 统一资产模型 | ✅ 强化 QM Workspace Layers |
| P2 | 冷启动对话提炼 | 导入 Session（§2.7） | 导入历史 IM 对话 | ⚠️ 需发布门 |
| P2 | 记忆 Benchmark | PersonaMem（§2.8） | 记忆有效性度量 | ✅ 验证手段 |

## 5. 与 QM 基准的互补关系

| 主题 | QM 提供 | TAM 提供 | 关系 |
|---|---|---|---|
| 记忆分层与召回 | 无（per-scope memory，无分层提取） | L0-L3 Pipeline + 混合召回 + 预算 | **TAM 补 QM 之缺** |
| 知识形态 | 无 | Wiki + CodeGraph 按需工具 | **TAM 新增** |
| 资产可见性 / 层级共享 | Workspace Layers（qm-benchmark §2.6） | private/team/restricted/agent | **互相印证，合并落地** |
| Skill 生命周期 | review→promote（qm-benchmark §2.7） | draft→review→publish + 版本/校验 | **互相印证** |
| 信任模型 | 与 Bycall 同构（直接映射） | 比 Bycall 宽松（需治理化改造） | QM 直接用，TAM 套门用 |

> 二者不冲突：QM 给的是"治理机制如何更工程化"，TAM 给的是"认知能力如何落地"。ADR-0006 把两者
> 统一在"策略平面严（QM 同构）+ 认知平面开放受治理（TAM 结构 + 发布门）"的两平面模型里。
