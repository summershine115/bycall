# Bycall 决策登记册（Decision Registry）

> **本文件是项目所有架构/产品决策的唯一入口。** 查找决策先看这里；写新决策遵守 §4 流程。
> 状态约定：✅ 已采纳（含已落地） · 🕐 提议中（proposed） · ⚠️ 待拍板（open） · 🚫 已否决 · ♻️ 已替代

## 1. ADR 索引（正式决策记录）

| 编号 | 决策 | 状态 | 日期 | 文件 |
|------|------|------|------|------|
| ADR-0001 | 抽象 VectorStore 接口（可切换后端） | ✅ 已采纳 | 2026-07-08 | `0001-vectorstore-abstraction.md` |
| ADR-0002 | 渐进式引入 LangGraph（编排+工具先行，Checkpointer 留后续） | ✅ 已采纳 | 2026-07-15 | `0002-langgraph-incremental.md` |
| ADR-0003 | 抽象文档解析引擎（可切换） | ✅ 已采纳 | 2026-07-27 | `0003-parse-engine-abstraction.md` |
| ADR-0004 | 单组织私有部署优先（tenant 预留不强制落地） | ✅ 已采纳 | 2026-07-30 | `0004-single-tenant-priority.md` |
| ADR-0005 | 抽象统一 ABAC Policy 接口（自研，暂不引 OPA/Cedar） | ✅ 已采纳 | 2026-07-30 | `0005-abac-policy-abstraction.md` |
| ADR-0006 | 持久化任务系统（SQLModel 增量，不引独立工作流引擎） | ✅ 已采纳 | 2026-07-30 | `0006-task-system-incremental.md` |
| ADR-0007 | 受治理认知——两平面信任模型（策略平面严苛 / 认知平面开放+发布门） | ✅ 已采纳 | 2026-08-06 | `0007-governed-cognition-two-plane.md` |
| ADR-0008 | 持久化任务执行核心 S1a（`run_tool_loop` 作 Step 内核 + typed event 统一） | ✅ 已采纳 | 2026-08-06 | `0008-agent-task-execution.md` |

## 2. 正文中的已采纳决策（未单独成 ADR，位置即权威）

这些决策已拍板并记录在正文文档中，保持原位不迁移，此处仅登记状态：

| 决策 | 状态 | 位置 |
|------|------|------|
| QM 工程化借鉴五件套：消息幂等去重 / 会话租约 / 压缩异步化已落地（2026-08-12，merge 56605ba）；operator error log / 成本预算 / 推送 outbox 未落地 | ✅ 部分落地 | `product-design.md` §6.1、`qm-benchmark.md` §6 |
| 群聊共同授权（audience floor：交集放行/并集拒绝） | 📋 已规划（M3 多用户会话） | `qm-benchmark.md` §2.1、`product-design.md` §2.4 |
| HITL 审批授权三档（once/session/always） | 📋 已规划（M4） | `product-design.md` §6.1 |
| 安全姿态分级（strict/auto/dangerous，org 为地板） | 📋 已规划（P1） | `qm-benchmark.md` §2.2、§4 |
| M4 设计红线：grant 变更/身份冒充/命令审批不给 agent 入口 | ✅ 已采纳（设计约束） | `product-design.md` §8 |
| 运行时能力主动披露且不可提权（Runtime Context） | ✅ 已落地（红线 #9） | `architecture.md` 红线 #9 |
| 失败时保持授权状态（向量侧先行，失败不提交元数据/ACL 变更） | ✅ 已落地（红线 #6） | `architecture.md` 红线 #6 |
| SKILL 信任定位：admin 管控的可信配置，合法注入 system（软隔离） | ✅ 已落地 | `product-design.md` §2.4、红线 #7 |

## 3. 开放决策清单（待拍板）

合并自 `product-design.md` §7、`product-strategy.md` §8、`specs/agent-framework-upgrade.md` 假设。
**拍板后移入 §1 或 §2**，不留悬念。

| # | 决策 | 状态/当前倾向 | 来源 |
|---|------|--------------|------|
| 1 | 身份映射模式：邀请制 vs 自注册+待审批 | 倾向邀请制（代码已按此实现），待正式确认 | design §7 |
| 2 | 未受邀员工接入行为：拒绝+引导 vs 自动建待审记录 | 倾向拒绝+引导（已实现），待正式确认 | design §7 |
| 3 | 权限缓存：会话级 vs 每请求实时 | 会话级（已实现），待正式确认 | design §7 |
| 4 | Admin 认证：账号密码（已实现）vs 企业 SSO | SSO 排入 M5，未排期 | design §7 / strategy §8 |
| 5 | per-session 模型选择：全局激活+会话覆盖 | 结构已预留，运行时启用待排期（M3） | design §7 |
| 6 | 首个付费行业：制造业设备运维/工业售后？ | 待定（有倾向，未拍板） | strategy §8-1 |
| 7 | 行业包是否作为版本化、可导入导出的正式产品资产 | 待定 | strategy §8-5 |
| 8 | M5 收口与行业试点之间的资源比例与验收门槛 | 待定 | strategy §8-6 |
| 9 | Skill 晋升闸门复用 `ApprovalRequest`（草稿→审→晋升） | 倾向复用（S3 落地），待确认 | spec 假设 1 |
| 10 | skill 自动提议「成功」判定：同序列重复 ≥ N 次（N 可配，默认 3） | 待定（S3） | spec 假设 6 |

**已由 ADR 拍板、从开放清单移出的**（历史决策点 → 决策记录）：

| 原决策点 | 结论 | 记录 |
|---------|------|------|
| 单组织私有部署 vs SaaS 多租户 | 单组织私有部署优先，tenant 预留不强制落地 | ADR-0004 |
| ABAC 自研 vs 引入 OPA/Cedar | 自研统一 Policy 接口 | ADR-0005 |
| 任务系统 SQLModel 增量 vs 独立工作流引擎 | SQLModel 增量 | ADR-0006 |
| 记忆归谁：LLM 自主 vs 组织管控 | 两平面：策略平面不变，认知平面开放+发布门 | ADR-0007 |
| 执行层承载：落在 Task/Step/Run；不引入 PI TS 栈；L3 记忆降权进 user | 按 spec 假设 3/4/5 实现 | ADR-0008、ADR-0007 |

## 4. 新决策流程约定

1. **新决策先写 ADR**：`docs/decisions/NNNN-<slug>.md`，编号递增不复用；状态先标 🕐 提议中。
2. **拍板后更新状态**：评审通过或代码落地后标 ✅ 已采纳，并登记到 §1 索引。
3. **正文只留指针**：产品/架构文档写「见 ADR-NNNN」，不在正文里写大段决策论证。
4. **被替代/否决**：标 ♻️ 已替代（指向替代者）或 🚫 已否决，不删除——决策历史也是资产。
5. **待定项集中**：所有待拍板事项进 §3，拍板即移出，杜绝「散落三处各说各话」。

### ADR 模板

```markdown
# ADR-NNNN：<一句话标题>

- **状态**：🕐 提议中 / ✅ 已采纳
- **日期**：YYYY-MM-DD
- **背景**：为什么需要这个决策（问题，不是方案）
- **关联**：相关 ADR / 文档章节

## 背景
## 决策
## 备选方案
## 后果
```

## 5. 正文文档状态一览（统一状态栏）

| 文档 | 状态 | 说明 |
|------|------|------|
| `docs/product-design.md` | 演进中 | 产品形态设计（≈PRD）。核心章节已随实现演进；**§6 M1 验收标准已过时**（能力早已超出）；§7 待定项见 §3 |
| `docs/product-strategy.md` | 建议稿（待评审） | 战略判断。§8 决策点 2/3/4 已转 ADR-0004/0005/0006，其余见 §3 |
| `docs/architecture.md` | 随实现演进 | 分层架构 + 9 条红线（当前约束，不可违背） |
| `docs/qm-benchmark.md` | 调研记录（已采纳） | QM 对标；借鉴项映射见 product-design §6.1 |
| `docs/tencent-agent-memory-benchmark.md` | 调研记录（已采纳） | TAM 对标；已升华为 ADR-0007 |
| `docs/specs/agent-framework-upgrade.md` | 已采纳 | S1a 已落地；S1b–S4 待排期（切片见 §8） |
| `tasks/plan.md` / `tasks/todo.md` | 随任务状态 | S1a 实施计划与任务清单，**已完成** |
| `CHANGELOG.md` | 版本记录 | 用户可见变更流水账，不承载「为什么」 |
